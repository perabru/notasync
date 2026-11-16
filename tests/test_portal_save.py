"""Exact Univap Save-confirmation contracts using synthetic DOM only."""
import os
import shutil
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from playwright.async_api import async_playwright

from notasync.portal import PortalAdapter, PortalError, SAVE_CONFIRMATION


BASE = "https://portal.univap.br/fixture"


class NativeSaveDialogTests(unittest.IsolatedAsyncioTestCase):
    def make_dialog(self, *, message=SAVE_CONFIRMATION, kind="confirm", url=BASE):
        return SimpleNamespace(message=message, type=kind, page=SimpleNamespace(url=url), accept=AsyncMock(), dismiss=AsyncMock())

    async def test_exact_native_confirmation_is_accepted_only_while_saving(self):
        adapter = PortalAdapter()
        adapter._saving = True
        dialog = self.make_dialog()
        await adapter._handle_dialog(dialog)
        dialog.accept.assert_awaited_once()
        dialog.dismiss.assert_not_awaited()
        self.assertTrue(adapter._save_confirmation_seen)
        self.assertFalse(adapter._unexpected_dialog)

    async def test_same_confirmation_outside_save_is_dismissed_and_stops_run(self):
        adapter = PortalAdapter()
        dialog = self.make_dialog()
        await adapter._handle_dialog(dialog)
        dialog.accept.assert_not_awaited()
        dialog.dismiss.assert_awaited_once()
        with self.assertRaisesRegex(PortalError, "diálogo inesperado"):
            adapter._assert_dialog_safe()

    async def test_undo_foreign_type_or_foreign_origin_is_never_accepted(self):
        cases = [
            {"message": "Deseja desfazer as alterações efetuadas?"},
            {"message": "Deseja excluir todas as notas?"},
            {"kind": "alert"},
            {"url": "https://unrelated.example/"},
        ]
        for case in cases:
            with self.subTest(case=case):
                adapter = PortalAdapter()
                adapter._saving = True
                dialog = self.make_dialog(**case)
                await adapter._handle_dialog(dialog)
                dialog.accept.assert_not_awaited()
                dialog.dismiss.assert_awaited_once()
                self.assertTrue(adapter._unexpected_dialog)

    async def test_duplicate_native_confirmation_is_never_accepted_twice(self):
        adapter = PortalAdapter()
        adapter._saving = True
        first, second = self.make_dialog(), self.make_dialog()
        await adapter._handle_dialog(first)
        await adapter._handle_dialog(second)
        first.accept.assert_awaited_once()
        second.accept.assert_not_awaited()
        second.dismiss.assert_awaited_once()
        self.assertTrue(adapter._unexpected_dialog)


LIST = """<!doctype html><html><body><h1>Turmas do Docente</h1>
<table id=grdTurmas><thead><tr><th>Disciplina</th><th>Nome</th><th>Turma</th><th>Período Letivo</th></tr></thead>
<tbody><tr><td>TESTE-RED</td><td><a href='/fixture?grades=1'>Redes de Computadores</a></td><td>TESTE-1A</td><td>Ano 2026</td></tr></tbody></table>
</body></html>"""


def grade_page(mode):
    headers = ["Aluno", "Código", "Situação", "Curso", "A11", "AT1", "AR1", "MB1", "A12", "AT2", "AR2", "MB2", "A13", "AT3", "AR3", "MB3", "A14", "AT4", "AR4", "MB4", "MBF", "EF", "NC"]
    values = ["Aluno Teste", "90000001", "Matriculado", "Curso Teste", "8.0", "6.5", "4.5", "7.0", "8.0", "6.5", "4.5", "7.0", "8.0", "6.5", "4.5", "7.0", "8.0", "<input id=grade value=7.0>", "4.5", "7.0", "7.0", "", ""]
    prompt = SAVE_CONFIRMATION if mode not in ("wrong-ext", "wrong-native") else "Deseja desfazer as alterações efetuadas?"
    button = "function showSave(){document.getElementById('confirmation').style.display='block';}" if mode.endswith("ext") else "function showSave(){if(confirm(" + repr(prompt) + "))persist();}"
    modal = "<div class='x-window x-window-dlg' id='confirmation' role='dialog' style='display:none'><div class='x-window-header'><span class='x-window-header-text'>Confirmação</span></div><div class='x-window-body'><span class='ext-mb-text'>" + prompt + "</span></div><div class='x-window-footer'><button class='x-btn-text' id='yes' onclick='persist()'>Sim</button><button class='x-btn-text' id='no' onclick=\"document.getElementById('confirmation').style.display='none'\">Não</button></div></div>"
    return "<!doctype html><html><body><h1>Lançar Notas (Incluir ou Alterar)</h1><p>Redes de Computadores | TESTE-1A | Ano 2026</p><a href='/fixture'>Selecionar outra turma</a><button class='x-btn-text icon-save' onclick='showSave()'>Salvar</button><div id='gridNotas' class='x-grid-panel'><div class='x-grid3-header'><table><thead><tr>" + "".join("<th>" + h + "</th>" for h in headers) + "</tr></thead></table></div><div class='x-grid3-scroller'><div class='x-grid3-row'><table><tbody><tr>" + "".join("<td>" + v + "</td>" for v in values) + "</tr></tbody></table></div></div></div>" + modal + "<script>function persist(){localStorage.setItem('saved-note',document.getElementById('grade').value);document.getElementById('confirmation').style.display='none';}let saved=localStorage.getItem('saved-note');if(saved!==null)document.getElementById('grade').value=saved;" + button + "</script></body></html>"


@unittest.skipUnless(os.environ.get("NOTASYNC_BROWSER_TESTS") == "1", "Use NOTASYNC_BROWSER_TESTS=1 para as confirmações de navegador locais.")
class SaveBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pw = await async_playwright().start()
        options = {"headless": True}
        browser_path = os.environ.get("NOTASYNC_TEST_BROWSER") or shutil.which("chromium")
        if browser_path:
            options["executable_path"] = browser_path
        self.browser = await self.pw.chromium.launch(**options)
        self.context = await self.browser.new_context()
        self.mode = "expected-ext"

        async def fixture(route):
            body = grade_page(self.mode) if "grades=1" in route.request.url else LIST
            await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=body)

        await self.context.route("**/*", fixture)
        self.page = await self.context.new_page()
        self.adapter = PortalAdapter({"target_year": 2026, "validated": True, "offerings_container": "#grdTurmas", "grades_container": "#gridNotas", "another_class_selector": "", "offerings_first_selector": "", "offerings_next_selector": "", "timeout_ms": 2000})
        self.adapter.page = self.page
        self.adapter.context = self.context
        self.page.on("dialog", self.adapter._handle_dialog)
        await self.page.goto(BASE)

    async def asyncTearDown(self):
        await self.browser.close()
        await self.pw.stop()

    async def execute_change(self):
        offerings = await self.adapter.discover()
        metadata = await self.adapter.inspect_offering(offerings[0])
        self.assertEqual(len(metadata["headers"]), 23)
        self.assertEqual(metadata["assessments"], ["A11", "AT1", "A12", "AT2", "A13", "AT3", "A14", "AT4"])
        return await self.adapter.apply_offering(offerings[0], [{"registration": "90000001", "old": 7, "new": 8.3, "column": "AT4"}])

    async def test_exact_extjs_save_confirmation_is_confirmed_and_reloaded(self):
        result = await self.execute_change()
        self.assertEqual(result[0]["status"], "confirmed")
        self.assertTrue(result[0]["verified"])
        self.assertEqual(await self.page.locator("#grade").input_value(), "8.3")
        cells = self.page.locator(".x-grid3-row td")
        self.assertEqual(await cells.nth(18).inner_text(), "4.5")
        self.assertEqual(await cells.nth(19).inner_text(), "7.0")

    async def test_exact_native_save_confirmation_is_confirmed_and_reloaded(self):
        self.mode = "expected-native"
        result = await self.execute_change()
        self.assertEqual(result[0]["status"], "confirmed")
        self.assertTrue(result[0]["verified"])

    async def test_unexpected_extjs_prompt_never_clicks_sim_or_reports_success(self):
        self.mode = "wrong-ext"
        result = await self.execute_change()
        self.assertEqual(result[0]["status"], "unknown")
        self.assertFalse(result[0]["verified"])
        self.assertTrue(await self.page.locator("#confirmation").is_visible())
        self.assertIsNone(await self.page.evaluate("localStorage.getItem('saved-note')"))

    async def test_unexpected_native_prompt_is_dismissed_and_never_reports_success(self):
        self.mode = "wrong-native"
        result = await self.execute_change()
        self.assertEqual(result[0]["status"], "unknown")
        self.assertFalse(result[0]["verified"])
        self.assertIsNone(await self.page.evaluate("localStorage.getItem('saved-note')"))

    async def test_canceling_modal_manually_does_not_persist(self):
        offerings = await self.adapter.discover()
        await self.adapter.read_offering(offerings[0])
        await self.page.locator("#grade").fill("8.3")
        await self.page.locator("button.icon-save").click()
        await self.page.locator("#no").click()
        await self.page.reload()
        self.assertEqual(await self.page.locator("#grade").input_value(), "7.0")
        self.assertIsNone(await self.page.evaluate("localStorage.getItem('saved-note')"))


if __name__ == "__main__":
    unittest.main()
