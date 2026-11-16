"""Synthetic DOM tests. These do not contact the real Univap portal."""
import unittest
import shutil
import os
import copy
from unittest.mock import AsyncMock, Mock
from types import SimpleNamespace
from urllib.parse import urlparse

from playwright.async_api import async_playwright

from notasync.portal import ASSESSMENT_CODES, PortalAdapter, PortalError, grade, registration


BASE = "https://portal.univap.br/fixture"
LIST = """<!doctype html><html><body>
<h1>Turmas do Docente</h1>
<table id=offerings><thead><tr><th>Disciplina</th><th>Nome</th><th>Turma</th><th>Período Letivo</th></tr></thead><tbody id=rows></tbody></table>
<button id=first onclick="render(1)">Primeira</button><button id=next onclick="render(2)">Próxima</button><p id=count></p>
<script>
function render(p){document.getElementById('rows').innerHTML=p===1?'<tr><td>R1</td><td><a href="/fixture?offering=1">Redes de Computadores</a></td><td>INF-1A</td><td>Ano 2026</td></tr>':'<tr><td>B1</td><td><a href="/fixture?offering=2">Banco de Dados</a></td><td>INF-2A</td><td>Ano 2026</td></tr>';document.getElementById('first').disabled=p===1;document.getElementById('next').disabled=p===2;document.getElementById('count').textContent='Exibindo registros '+p+' - '+p+' de 2';}render(1);
</script></body></html>"""


def grades_html(offering):
    discipline, turma = ("Redes de Computadores", "INF-1A") if offering == "1" else ("Banco de Dados", "INF-2A")
    return f"""<!doctype html><html><body>
<h1>Lançar Notas</h1><p>{discipline} | Turma {turma} | Ano 2026</p>
<button onclick="location.href='/fixture'">Selecionar outra turma</button>
<button id=save onclick="document.querySelectorAll('#grades input').forEach(i=>localStorage.setItem(i.id,i.value))">Salvar</button>
<table id=grades><thead><tr><th>Aluno</th><th>Código</th><th>Situação</th><th>A14</th><th>C-AT4</th><th>AT4</th></tr></thead>
<tbody><tr><td>Aluno Exemplo</td><td>50260001</td><td>Matriculado</td><td>6.5</td><td>S</td><td><input id=grade1 value=7.0></td></tr>
<tr><td>Aluna Exemplo</td><td>50260002</td><td>Cancelado</td><td>9.5</td><td>S</td><td><input id=grade2 value=8.0></td></tr></tbody></table>
<script>document.querySelectorAll('#grades input').forEach(i=>{{let v=localStorage.getItem(i.id);if(v!==null)i.value=v;}});</script>
</body></html>"""


class BasicTests(unittest.TestCase):
    def test_live_adapter_rejects_local_or_foreign_targets(self):
        adapter = PortalAdapter()
        adapter._check_target("https://portal.univap.br/DOnline/DOnline/notas/TDOL008D.tp")
        for url in ("http://127.0.0.1:8123/test-portal/session/login", "https://127.0.0.1/", "http://portal.univap.br/", "https://portal.univap.br.example.org/", "https://example.org/"):
            with self.subTest(url=url), self.assertRaises(PortalError):
                adapter._check_target(url)
        configured = PortalAdapter({"login_url": "http://127.0.0.1:8123/test-portal/session/login"})
        with self.assertRaises(PortalError):
            configured._check_target(configured.config["login_url"])

    def test_frame_allowlist_has_no_configuration_override(self):
        adapter = PortalAdapter({"frame_allowed": "*", "allowed_origin": "http://127.0.0.1:8123"})
        self.assertTrue(adapter._frame_allowed("about:blank"))
        self.assertTrue(adapter._frame_allowed("https://portal.univap.br/frame"))
        for url in ("http://127.0.0.1:8123/test-portal/session/login", "https://example.org/", "https://portal.univap.br.example.org/"):
            self.assertFalse(adapter._frame_allowed(url))
        self.assertEqual(adapter._launch_options(), {"headless": False})

    def test_registration_and_grade(self):
        self.assertEqual(registration("5.0260001E7"), "50260001")
        self.assertEqual(registration("0050260001"), "0050260001")
        self.assertEqual(registration("Infinity"), "")
        self.assertEqual(registration("NaN"), "")
        self.assertEqual(grade("8,3"), 8.3)
        self.assertIsNone(grade(""))
        for text in ("S", "11", "NaN", "-1"):
            with self.assertRaises(PortalError):
                grade(text)

    def test_offering_identity_ignores_volatile_links(self):
        first = {"code": "R1", "class_name": "INF-1A", "discipline": "Redes", "year_text": "Ano 2026", "href": "a?session=old"}
        second = {**first, "href": "a?session=new"}
        self.assertEqual(PortalAdapter._offering_id(first), PortalAdapter._offering_id(second))
        self.assertNotEqual(PortalAdapter._offering_id(first), PortalAdapter._offering_id({**first, "class_name": "INF-1B"}))

    def test_only_requested_assessments_are_allowed(self):
        adapter = PortalAdapter()
        self.assertEqual(ASSESSMENT_CODES, ("A11", "AT1", "A12", "AT2", "A13", "AT3", "A14", "AT4"))
        for code in ASSESSMENT_CODES:
            self.assertEqual(adapter._assessment(code), code)
        for code in ("C-AT4", "MB1", "Média Final", "MBF", "AR4", "EF", ""):
            with self.assertRaises(PortalError):
                adapter._assessment(code)


class FrameBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def make_adapter(self, allowed):
        foreign = SimpleNamespace(url="https://unrelated.example/frame", locator=Mock(), evaluate=AsyncMock())
        adapter = PortalAdapter()
        adapter.page = SimpleNamespace(url=BASE, frames=[foreign, allowed], is_closed=lambda: False)
        return adapter, foreign

    async def test_login_fields_are_never_queried_in_foreign_frames(self):
        candidate = SimpleNamespace(is_visible=AsyncMock(return_value=True))
        locator = SimpleNamespace(count=AsyncMock(return_value=1), nth=lambda index: candidate)
        allowed = SimpleNamespace(url=BASE, locator=Mock(return_value=locator))
        adapter, foreign = self.make_adapter(allowed)
        frame, field = await adapter._unique_selector("#username")
        self.assertIs(frame, allowed)
        self.assertIs(field, candidate)
        foreign.locator.assert_not_called()

    async def test_grade_extraction_skips_foreign_frames(self):
        data = {"container": "#grade", "headers": ["Aluno", "Código", "AT4"], "rows": []}
        allowed = SimpleNamespace(url=BASE, evaluate=AsyncMock(return_value=[data]))
        adapter, foreign = self.make_adapter(allowed)
        frame, found = await adapter._extract("grades")
        self.assertIs(frame, allowed)
        self.assertIs(found, data)
        foreign.evaluate.assert_not_awaited()

    async def test_navigation_skips_foreign_frames(self):
        candidate = SimpleNamespace(is_visible=AsyncMock(return_value=True), inner_text=AsyncMock(return_value="Selecionar outra turma"), click=AsyncMock())
        locator = SimpleNamespace(count=AsyncMock(return_value=1), nth=lambda index: candidate)
        allowed = SimpleNamespace(url=BASE, locator=Mock(return_value=locator))
        adapter, foreign = self.make_adapter(allowed)
        await adapter._text_control("SELECIONAR OUTRA TURMA")
        candidate.click.assert_awaited_once()
        foreign.locator.assert_not_called()

    async def test_metadata_never_reads_foreign_frames(self):
        allowed = SimpleNamespace(url=BASE, evaluate=AsyncMock(return_value="INF-1A Redes 2026"))
        adapter, foreign = self.make_adapter(allowed)
        await adapter._verify_metadata({"class_name": "INF-1A", "discipline": "Redes", "year": 2026})
        foreign.evaluate.assert_not_awaited()


@unittest.skipUnless(os.environ.get("NOTASYNC_BROWSER_TESTS") == "1", "Use NOTASYNC_BROWSER_TESTS=1 em um computador com navegador para os testes DOM locais.")
class PortalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pw = await async_playwright().start()
        options = {"headless": True}
        if os.environ.get("NOTASYNC_TEST_BROWSER"):
            options["executable_path"] = os.environ["NOTASYNC_TEST_BROWSER"]
        elif shutil.which("chromium"):
            options["executable_path"] = shutil.which("chromium")
        self.browser = await self.pw.chromium.launch(**options)
        self.context = await self.browser.new_context()
        async def fixture(route):
            parsed = urlparse(route.request.url)
            self.assertEqual(parsed.hostname, "portal.univap.br")
            html = grades_html("2" if "offering=2" in parsed.query else "1") if parsed.query else LIST
            await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)
        await self.context.route("**/*", fixture)
        page = await self.context.new_page()
        await page.goto(BASE)
        self.adapter = PortalAdapter({"target_year": 2026, "headless": True, "browser_channel": "", "validated": True, "offerings_container": "#offerings", "grades_container": "#grades", "offerings_next_selector": "#next", "offerings_first_selector": "#first", "another_class_selector": "", "save_selector": ""})
        self.adapter.page = page
        self.adapter.context = self.context

    async def asyncTearDown(self):
        await self.browser.close()
        await self.pw.stop()

    async def test_pagination_read_write_and_persistence(self):
        offerings = await self.adapter.discover()
        self.assertEqual(len(offerings), 2)
        rows = await self.adapter.read_offering(offerings[0])
        self.assertEqual(rows[0]["grade"], 7.0)
        self.assertEqual(rows[0]["registration"], "50260001")
        changes = [{"registration": "50260001", "old": 7, "new": 8.3}]
        result = await self.adapter.apply_offering(offerings[0], changes)
        self.assertEqual(result[0]["status"], "confirmed")
        # A14 and adjacent C-AT4 are untouched.
        self.assertEqual(await self.adapter.page.locator("#grades tbody tr").first.locator("td").nth(3).inner_text(), "6.5")
        self.assertEqual(await self.adapter.page.locator("#grades tbody tr").first.locator("td").nth(4).inner_text(), "S")

    async def test_stale_conflict_stops_before_edit(self):
        offerings = await self.adapter.discover()
        with self.assertRaises(PortalError):
            await self.adapter.apply_offering(offerings[0], [{"registration": "50260001", "old": 6, "new": 9}])
        self.assertEqual(await self.adapter.page.locator("#grade1").input_value(), "7.0")

    async def test_cancelled_student_is_not_edited(self):
        offerings = await self.adapter.discover()
        with self.assertRaises(PortalError):
            await self.adapter.apply_offering(offerings[0], [{"registration": "50260002", "old": 8, "new": 9}])
        self.assertEqual(await self.adapter.page.locator("#grade2").input_value(), "8.0")

    async def test_adjacent_header_is_not_accepted(self):
        offerings = await self.adapter.discover()
        await self.adapter.read_offering(offerings[0])
        await self.adapter.page.locator("#grades thead th").last.evaluate("e=>e.textContent='C-AT4'")
        with self.assertRaises(PortalError):
            await self.adapter._extract("grades")

    async def test_unverified_write_not_reported_as_success(self):
        offerings = await self.adapter.discover()
        await self.adapter.read_offering(offerings[0])
        async def no_save():
            pass
        self.adapter._save = no_save
        result = await self.adapter.apply_offering(offerings[0], [{"registration": "50260001", "old": 7, "new": 9}])
        self.assertEqual(result[0]["status"], "unknown")
        self.assertFalse(result[0]["verified"])


class TransactionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.adapter = PortalAdapter({"target_year": 2026, "validated": True})
        self.adapter.page = type("Page", (), {"reload": AsyncMock()})()
        self.rows = [{"registration": "50260001", "name": "Aluno Exemplo", "status": "Matriculado", "grade": 7.0}, {"registration": "50260002", "name": "Aluna Exemplo", "status": "Cancelado", "grade": 8.0}]
        self.saved = copy.deepcopy(self.rows)
        self.offering = {"id": "synthetic", "class_name": "INF-1A", "discipline": "Redes", "year": 2026}
        self.adapter._open_offering = AsyncMock()
        self.adapter._wait_extract = AsyncMock()
        self.adapter._verify_metadata = AsyncMock()
        async def collect(assessment=None):
            return copy.deepcopy(self.rows)
        async def find(ra, assessment=None):
            return None, copy.deepcopy(next(r for r in self.rows if r["registration"] == ra))
        async def edit(frame, row, new, assessment=None):
            next(r for r in self.rows if r["registration"] == row["registration"])["grade"] = new
        async def save():
            self.saved = copy.deepcopy(self.rows)
        async def reload(**kwargs):
            self.rows = copy.deepcopy(self.saved)
        self.adapter._collect = collect
        self.adapter._find_cell = find
        self.adapter._edit = AsyncMock(side_effect=edit)
        self.adapter._save = AsyncMock(side_effect=save)
        self.adapter.page.reload = AsyncMock(side_effect=reload)

    async def test_success_requires_persisted_read(self):
        result = await self.adapter.apply_offering(self.offering, [{"registration": "50260001", "old": 7, "new": 8.3}])
        self.assertEqual(result[0]["status"], "confirmed")
        self.adapter._save.assert_awaited_once()
        self.adapter.page.reload.assert_awaited_once()
        self.assertEqual(self.rows[1]["grade"], 8.0)

    async def test_conflict_blocks_before_any_edit(self):
        with self.assertRaises(PortalError):
            await self.adapter.apply_offering(self.offering, [{"registration": "50260001", "old": 6, "new": 9}])
        self.adapter._edit.assert_not_awaited()
        self.adapter._save.assert_not_awaited()

    async def test_cancelled_blocks_before_any_edit(self):
        with self.assertRaises(PortalError):
            await self.adapter.apply_offering(self.offering, [{"registration": "50260002", "old": 8, "new": 9}])
        self.adapter._edit.assert_not_awaited()
        self.adapter._save.assert_not_awaited()

    async def test_failed_save_is_unknown_and_never_retried(self):
        self.adapter._save = AsyncMock(side_effect=RuntimeError("network disconnected"))
        result = await self.adapter.apply_offering(self.offering, [{"registration": "50260001", "old": 7, "new": 9}])
        self.assertEqual(result[0]["status"], "unknown")
        self.adapter._save.assert_awaited_once()

    async def test_unsaved_value_is_not_success(self):
        self.adapter._save = AsyncMock()
        result = await self.adapter.apply_offering(self.offering, [{"registration": "50260001", "old": 7, "new": 9}])
        self.assertEqual(result[0]["status"], "unknown")
        self.assertFalse(result[0]["verified"])

    async def test_bad_grade_or_duplicate_student_never_edits(self):
        for changes in ([{"registration": "50260001", "old": 7, "new": 11}], [{"registration": "50260001", "old": 7, "new": 9}] * 2):
            with self.assertRaises(PortalError):
                await self.adapter.apply_offering(self.offering, changes)
        self.adapter._edit.assert_not_awaited()

    async def test_selected_bimester_is_used_for_entire_write(self):
        result = await self.adapter.apply_offering(self.offering, [{"registration": "50260001", "old": 7, "new": 8.3, "column": "AT2"}], assessment="AT2")
        self.assertEqual(result[0]["status"], "confirmed")
        self.adapter._open_offering.assert_awaited_once_with(self.offering, "AT2")
        self.assertEqual(self.adapter._edit.await_args.args[-1], "AT2")

    async def test_mixed_assessment_batch_is_never_edited(self):
        with self.assertRaises(PortalError):
            await self.adapter.apply_offering(self.offering, [{"registration": "50260001", "old": 7, "new": 9, "column": "AT4"}], assessment="AT2")
        self.adapter._edit.assert_not_awaited()
        self.adapter._save.assert_not_awaited()


class ExtJSActionTests(unittest.IsolatedAsyncioTestCase):
    async def test_inspect_finds_exact_headers_without_requiring_at4(self):
        adapter = PortalAdapter({"grade_column": "AT4"})
        offering = {"id": "fixture", "class_name": "INF-1A", "discipline": "Redes", "year": 2026}
        adapter._open_offering = AsyncMock()
        adapter._wait_extract = AsyncMock(return_value=(None, {"headers": ["Aluno", "Código", "C-AT4", "AT1", "A11", "MB1", "A12", "A12", "Média Final"]}))
        adapter._edit = AsyncMock()
        result = await adapter.inspect_offering(offering)
        self.assertEqual(result["assessments"], ["A11", "AT1"])
        adapter._open_offering.assert_awaited_once_with(offering, assessment="")
        adapter._edit.assert_not_awaited()

    async def test_single_click_opens_observed_editor(self):
        empty = SimpleNamespace(count=AsyncMock(return_value=0))
        inner = SimpleNamespace(count=AsyncMock(return_value=1), click=AsyncMock())
        cell = SimpleNamespace(locator=lambda selector: inner if selector == ".x-grid3-cell-inner" else empty, dblclick=AsyncMock())
        editor = SimpleNamespace(wait_for=AsyncMock(), count=AsyncMock(return_value=1), is_editable=AsyncMock(return_value=True), fill=AsyncMock(), press=AsyncMock())
        frame = SimpleNamespace(locator=lambda selector: cell if selector == "#observedCell" else editor)
        adapter = PortalAdapter()
        adapter._find_cell = AsyncMock(return_value=(None, {"grade": 8.3}))
        await adapter._edit(frame, {"cell_selector": "#observedCell", "registration": "50260001"}, 8.3)
        inner.click.assert_awaited_once()
        cell.dblclick.assert_not_awaited()
        editor.fill.assert_awaited_once_with("8.3")
        editor.press.assert_awaited_once_with("Tab")

    async def test_ext_ancestor_disabled_is_honored(self):
        locator = SimpleNamespace(is_enabled=AsyncMock(return_value=True), evaluate=AsyncMock(return_value=True))
        self.assertFalse(await PortalAdapter._control_enabled(locator))


if __name__ == "__main__":
    unittest.main()
