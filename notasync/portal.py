"""Observable browser adapter for the Univap Docente/Coordenador portal.

Login selectors, the authenticated teacher's ExtJS grade grid and its editor
were inspected against the live portal without changing grades. DOM extraction
was verified on 42 unique registrations. Coordinator-wide discovery and live
Save remain unverified. No internal HTTP endpoint or existing browser session
is used by this adapter.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import re
import unicodedata
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urljoin, urlparse


class PortalError(RuntimeError):
    """An actionable portal state; never include credentials in this message."""


ASSESSMENT_CODES = ("A11", "AT1", "A12", "AT2", "A13", "AT3", "A14", "AT4")
SAVE_CONFIRMATION = "Deseja salvar as alterações efetuadas?"


DEFAULT_CONFIG = {
    "login_url": "https://portal.univap.br/DOnline/DOnline/notas/TDOL008D.tp",
    "username_selector": "#username",
    "password_selector": "#password",
    "login_submit_selector": "#sendCredentials",
    "target_year": date.today().year,
    "grade_column": "AT4",
    "offerings_container": "#grdTurmas",
    "grades_container": "",
    "offerings_next_selector": "#grdTurmas button.x-tbar-page-next",
    "offerings_first_selector": "#grdTurmas button.x-tbar-page-first",
    "offerings_navigation_selector": "",
    "another_class_selector": '[id="blkTurma.lkmListarTurmas:text"]',
    "save_selector": "button.x-btn-text.icon-save",
    "editor_selector": ".x-grid-editor input.x-form-field:visible, .dhx_combo_edit:visible, .editable:visible",
    "timeout_ms": 15000,
    "max_offering_pages": 100,
    "max_scroll_steps": 300,
    "strict_metadata": True,
    "validated": False,
    "browser_channel": "msedge",
    "headless": False,
}


def normalize(value):
    value = unicodedata.normalize("NFKD", str(value or ""))
    return re.sub(r"\s+", " ", "".join(c for c in value if not unicodedata.combining(c))).strip().upper()


def registration(value):
    text = str(value or "").strip()
    if not text:
        return ""
    if re.fullmatch(r"\d+", text):
        return text
    try:
        number = Decimal(text.replace(",", "."))
        if number.is_finite() and number == number.to_integral() and number >= 0:
            return str(int(number))
    except InvalidOperation:
        pass
    # No approximate matching, punctuation removal, or guessed student identity.
    return text if re.fullmatch(r"\d+", text) else ""


def grade(value):
    text = str(value if value is not None else "").strip()
    if not text:
        return None
    try:
        number = Decimal(text.replace(",", "."))
    except InvalidOperation as exc:
        raise PortalError("O portal exibiu uma nota não numérica; confira a coluna selecionada.") from exc
    if not number.is_finite() or not Decimal(0) <= number <= Decimal(10):
        raise PortalError("O portal exibiu nota fora de 0 a 10; a turma não foi alterada.")
    return float(number)


def equal_grade(left, right):
    return grade(left) == grade(right)


# Read only DOM extraction, supporting ExtJS, ordinary tables and dhtmlx's
# separate header/body tables. CSS paths are fresh before each edit.
EXTRACT = r"""({kind,selector,column}) => {
 const norm=s=>(s||'').normalize('NFD').replace(/[\u0300-\u036f]/g,'').replace(/\s+/g,' ').trim().toUpperCase();
 const css=e=>{if(e.id)return '#'+CSS.escape(e.id);let p=[];while(e&&e.nodeType===1&&e!==document.documentElement){let n=e.tagName.toLowerCase(),i=1,s=e;while(s=s.previousElementSibling)if(s.tagName===e.tagName)i++;p.unshift(n+':nth-of-type('+i+')');e=e.parentElement;}return 'html > '+p.join(' > ');};
 const cells=r=>Array.from(r.children).filter(e=>['TD','TH'].includes(e.tagName)||e.getAttribute('role')==='gridcell'||e.getAttribute('role')==='columnheader');
 const txt=e=>{const i=e.querySelector('input:not([type=hidden]):not([type=password]),textarea,select');return i?i.value:(e.innerText||e.textContent||'').trim();};
 const wanted=kind==='offerings'?['DISCIPLINA','NOME','TURMA','PERIODO LETIVO']:['ALUNO','CODIGO',...(column?[norm(column)]:[])];
 let containers=selector?Array.from(document.querySelectorAll(selector)):Array.from(document.querySelectorAll('.x-grid-panel,.gridbox,.dhtmlxGrid,table,[role=grid]'));
 let matches=[];
 for(const root of containers){
   const rows=Array.from(root.querySelectorAll('tr,[role=row]')); let header=null,headers=[];
   for(const row of rows){const h=cells(row).map(e=>norm(txt(e)));if(wanted.every(w=>h.filter(x=>x===w).length===1)){header=row;headers=h;break;}}
   if(!header)continue;
   // ExtJS and dhtmlx keep header and data in separate nested tables.
   const actual=(!selector&&root.tagName==='TABLE'&&root.closest('.x-grid-panel,.gridbox,.dhtmlxGrid'))||root;
   const extRows=actual.querySelectorAll('.x-grid3-row table tr');
   const all=extRows.length?Array.from(extRows):Array.from(actual.querySelectorAll('tr,[role=row]'));
   const data=[];
   for(const row of all){
     if(row===header)continue;const cs=cells(row);if(cs.length!==headers.length)continue;
     const values=cs.map(txt);if(wanted.every(w=>values.map(norm).includes(w)))continue;
     const item={values,row_selector:css(row),cell_selectors:cs.map(css)};
     if(kind==='offerings'){
       const at=h=>headers.indexOf(h);item.class_name=values[at('TURMA')];item.discipline=values[at('NOME')];item.code=values[at('DISCIPLINA')];item.year_text=values[at('PERIODO LETIVO')];
       if(!item.class_name||!item.discipline)continue;
       const link='a,.t-hyperlink,[onclick]';
       const a=cs[at('NOME')].querySelector(link)||cs[at('DISCIPLINA')].querySelector(link);
       item.link_selector=a?css(a):null;item.href=a?a.getAttribute('href'):null;
     } else {
       const gi=column?headers.indexOf(norm(column)):-1;
       item.name=values[headers.indexOf('ALUNO')];item.registration=values[headers.indexOf('CODIGO')];item.status=headers.includes('SITUACAO')?values[headers.indexOf('SITUACAO')]:'';item.grade=gi>=0?values[gi]:null;item.cell_selector=gi>=0?css(cs[gi]):null;
       if(!item.registration||!item.name)continue;
     }
     data.push(item);
   }
   const scrollers=Array.from(actual.querySelectorAll('*')).filter(e=>e.clientHeight>0&&e.scrollHeight>e.clientHeight+5&&['auto','scroll'].includes(getComputedStyle(e).overflowY)).map(e=>({selector:css(e),top:e.scrollTop,height:e.clientHeight,total:e.scrollHeight}));
   matches.push({container:css(actual),headers,rows:data,scrollers,text:actual.innerText||actual.textContent||''});
 }
 // One .gridbox can be found again through its nested header table.
 const unique=Array.from(new Map(matches.map(m=>[m.container,m])).values());
 return unique;
}"""


class PortalAdapter:
    def __init__(self, config=None, audit_dir=None):
        self.config = {**copy.deepcopy(DEFAULT_CONFIG), **(config or {})}
        self.audit_dir = Path(audit_dir) if audit_dir else None
        self._pw = self.browser = self.context = self.page = None
        self._offerings = []
        self.last_warnings = []
        self._lock = asyncio.Lock()
        self._saving = False
        self._save_confirmation_seen = False
        self._unexpected_dialog = False
        self.progress_callback = None

    async def _report_progress(self, event):
        """Report counts only; a display failure must never retry or alter a Save."""
        callback = self.progress_callback
        if callback is None:
            return
        try:
            total, completed = event.get("total"), event.get("completed")
            if type(total) is not int or type(completed) is not int or total < 0:
                return
            phase, assessment = event.get("phase"), event.get("assessment")
            if phase not in ("fill", "save", "verify") or assessment not in ASSESSMENT_CODES:
                return
            # Do not forward a source record, registration, name or arbitrary
            # error text to the progress display.
            payload = {"phase": phase, "completed": min(total, max(0, completed)),
                       "total": total, "assessment": assessment}
            result = callback(payload)
            if inspect.isawaitable(result):
                await result
        except Exception:
            # This hook is observational. The caller still receives the real
            # write/verification outcome, including uncertain Save results.
            pass

    def _check_target(self, url):
        parsed = urlparse(url)
        allowed = urlparse(self.config["login_url"])
        if parsed.scheme != "https" or parsed.hostname != "portal.univap.br":
            raise PortalError("O acesso automático está restrito ao portal HTTPS oficial da Univap.")
        if allowed.hostname != parsed.hostname:
            raise PortalError("O endereço do portal mudou. Confira a configuração antes de entrar.")

    def _frame_allowed(self, url):
        """Keep frame access scoped to the production portal.

        The local training adapter overrides this method with its exact
        loopback origin and random session path. No configuration key can
        change the production origin allowlist.
        """
        return urlparse(url).hostname in (None, "portal.univap.br")

    def _launch_options(self):
        """A training subclass can pace its visible browser without changing live defaults."""
        return {"headless": bool(self.config["headless"])}

    async def open(self, username="", password=""):
        self._check_target(self.config["login_url"])
        if not self.page or self.page.is_closed():
            from playwright.async_api import async_playwright
            self._pw = await async_playwright().start()
            options = self._launch_options()
            channel = self.config.get("browser_channel")
            try:
                self.browser = await self._pw.chromium.launch(**options, **({"channel": channel} if channel else {}))
            except Exception:
                try:
                    self.browser = await self._pw.chromium.launch(**options)
                except Exception as exc:
                    await self._pw.stop()
                    self._pw = None
                    raise PortalError("Não foi possível abrir o navegador. Encerre o NotaSync e execute o instalador novamente para conferir o navegador e os componentes.") from exc
            self.context = await self.browser.new_context(accept_downloads=False)
            self.page = await self.context.new_page()
            self.page.set_default_timeout(int(self.config["timeout_ms"]))
            self._unexpected_dialog = False
            # Only the portal's exact Save confirmation, during this adapter's
            # Save click, can be accepted. All other native dialogs stop a run.
            self.page.on("dialog", self._handle_dialog)
            await self.page.goto(self.config["login_url"], wait_until="domcontentloaded")
        if username or password:
            if not username or not password:
                raise PortalError("Informe login e senha, ou deixe ambos vazios para entrar diretamente no navegador.")
            try:
                self._check_target(self.page.url)
                frame, _ = await self._unique_selector(self.config["password_selector"])
                await frame.locator(self.config["username_selector"]).fill(username)
                await frame.locator(self.config["password_selector"]).fill(password)
                await frame.locator(self.config["login_submit_selector"]).click()
                # The browser owns credentials only for this temporary session.
                username = password = ""
                try:
                    await frame.locator(self.config["password_selector"]).wait_for(state="hidden", timeout=self.config["timeout_ms"])
                except Exception:
                    return {"status": "waiting_login", "message": "O portal ainda solicita autenticação. Confira a mensagem no navegador; nenhum lançamento foi iniciado."}
            except PortalError:
                raise
            except Exception as exc:
                raise PortalError("O login não foi concluído. Confira o formulário aberto no navegador; nenhuma senha foi salva pelo programa.") from exc
        return {"status": "opened", "message": "Navegador aberto. Conclua a autenticação, se solicitada, e clique em localizar turmas."}

    async def close(self):
        try:
            if self.browser:
                await self.browser.close()
        finally:
            if self._pw:
                await self._pw.stop()
            self._pw = self.browser = self.context = self.page = None

    def _require_page(self):
        if not self.page or self.page.is_closed():
            raise PortalError("Abra o portal e entre com a conta do coordenador primeiro.")
        self._check_target(self.page.url)
        self._assert_dialog_safe()

    def _assert_dialog_safe(self):
        if self._unexpected_dialog:
            raise PortalError("O portal exibiu um diálogo inesperado. O envio foi interrompido; confira a sessão antes de repetir.")

    async def _handle_dialog(self, dialog):
        allowed_target = False
        try:
            self._check_target(dialog.page.url)
            allowed_target = True
        except (PortalError, AttributeError):
            pass
        if (self._saving and not self._save_confirmation_seen and allowed_target
                and dialog.type == "confirm" and normalize(dialog.message) == normalize(SAVE_CONFIRMATION)):
            self._save_confirmation_seen = True
            await dialog.accept()
        else:
            self._unexpected_dialog = True
            await dialog.dismiss()

    def _assessment(self, assessment=None):
        selected = normalize(self.config["grade_column"] if assessment is None else assessment)
        if selected not in ASSESSMENT_CODES:
            raise PortalError("Selecione uma avaliação válida: A11, AT1, A12, AT2, A13, AT3, A14 ou AT4.")
        return selected

    async def _unique_selector(self, selector):
        self._require_page()
        candidates = []
        for frame in self.page.frames:
            if not self._frame_allowed(frame.url):
                continue
            try:
                loc = frame.locator(selector)
                for i in range(await loc.count()):
                    if await loc.nth(i).is_visible():
                        candidates.append((frame, loc.nth(i)))
            except Exception:
                continue
        if len(candidates) != 1:
            raise PortalError("Não foi possível identificar um único controle no portal. Faça a configuração inicial do seletor correspondente.")
        return candidates[0]

    async def _text_control(self, pattern, hover=False):
        matches = []
        for frame in self.page.frames:
            if not self._frame_allowed(frame.url):
                continue
            for selector in ("a", "button", "input[type=button],input[type=submit]", "span"):
                loc = frame.locator(selector)
                for i in range(await loc.count()):
                    candidate = loc.nth(i)
                    if not await candidate.is_visible():
                        continue
                    text = await candidate.get_attribute("value") if selector.startswith("input") else await candidate.inner_text()
                    if re.fullmatch(pattern, normalize(text or "")):
                        matches.append(candidate)
                if matches:
                    break
        if len(matches) != 1:
            raise PortalError("O controle de navegação não foi reconhecido de forma única. Selecione a tela de notas no navegador ou configure o seletor.")
        await (matches[0].hover() if hover else matches[0].click())

    async def _extract(self, kind, assessment=None):
        self._require_page()
        selector = self.config["offerings_container" if kind == "offerings" else "grades_container"]
        selected = self.config["grade_column"] if assessment is None else assessment
        found = []
        for frame in self.page.frames:
            if not self._frame_allowed(frame.url):
                continue
            try:
                candidates = await frame.evaluate(EXTRACT, {"kind": kind, "selector": selector, "column": selected})
                found.extend((frame, candidate) for candidate in candidates)
            except Exception:
                continue
        if len(found) != 1:
            raise PortalError("A tabela de turmas não foi reconhecida. Abra a lista de turmas ou configure o seletor." if kind == "offerings" else f"Não foi encontrada uma tabela única com Aluno, Código{(' e a coluna exata ' + selected) if selected else ''}. Nenhuma nota foi alterada.")
        return found[0]

    async def _wait_extract(self, kind, assessment=None):
        for _ in range(max(1, int(self.config["timeout_ms"]) // 200)):
            try:
                frame, data = await self._extract(kind, assessment)
                if data["rows"]:
                    return frame, data
            except PortalError:
                pass
            await asyncio.sleep(0.2)
        return await self._extract(kind, assessment)

    async def _go_list(self):
        try:
            await self._extract("offerings")
            return
        except PortalError:
            pass
        explicit = self.config.get("another_class_selector")
        opened = False
        if explicit:
            try:
                _, locator = await self._unique_selector(explicit)
                await locator.click()
                opened = True
            except PortalError:
                pass  # Login landing page has no "another class" control.
        if not opened:
            try:
                await self._text_control("SELECIONAR OUTRA TURMA")
            except PortalError:
                nav = self.config.get("offerings_navigation_selector")
                if nav:
                    _, locator = await self._unique_selector(nav)
                    await locator.click()
                else:
                    await self._text_control("NOTAS E FALTAS", hover=True)
                    await self._text_control("LANCAR NOTAS")
        await self._wait_extract("offerings")

    async def _page_control(self, direction):
        explicit = self.config.get(f"offerings_{direction}_selector")
        if explicit:
            try:
                _, loc = await self._unique_selector(explicit)
            except PortalError:
                return False
            if await self._control_enabled(loc):
                previous = await self._list_signature()
                await loc.click()
                await self._wait_list_change(previous)
                return True
            return False
        # Only inspect DOM-provided labels; never guess click positions.
        matches = []
        labels = ("PRÓXIMA", "PROXIMA", "PRÓXIMO", "PROXIMO", "NEXT", "PÁGINA SEGUINTE", "PAGINA SEGUINTE", "PRÓXIMA PÁGINA", "PROXIMA PAGINA", "NEXT PAGE") if direction == "next" else ("PRIMEIRA", "PRIMEIRO", "FIRST", "PRIMEIRA PÁGINA", "PRIMEIRA PAGINA", "FIRST PAGE")
        for frame in self.page.frames:
            if not self._frame_allowed(frame.url):
                continue
            loc = frame.locator("a,button,input[type=button],img")
            for i in range(await loc.count()):
                item = loc.nth(i)
                if not await item.is_visible():
                    continue
                values = [await item.get_attribute(k) for k in ("title", "alt", "aria-label", "value")]
                values.append(await item.inner_text())
                if any(normalize(v) in {normalize(x) for x in labels} for v in values):
                    disabled = await item.get_attribute("disabled")
                    aria = await item.get_attribute("aria-disabled")
                    cls = normalize(await item.get_attribute("class"))
                    if disabled is None and aria != "true" and "DISABLED" not in cls and await self._control_enabled(item):
                        matches.append(item)
        if len(matches) == 1:
            previous = await self._list_signature()
            await matches[0].click()
            await self._wait_list_change(previous)
            return True
        return False

    async def _list_signature(self):
        _, data = await self._extract("offerings")
        return "|".join(self._offering_id(row) for row in data["rows"])

    async def _wait_list_change(self, previous):
        for _ in range(max(1, int(self.config["timeout_ms"]) // 150)):
            try:
                current = await self._list_signature()
                if current and current != previous:
                    return
            except PortalError:
                pass
            await asyncio.sleep(0.15)
        raise PortalError("A página de turmas não terminou de carregar. Aguarde no navegador e localize as turmas novamente.")

    @staticmethod
    async def _control_enabled(locator):
        if not await locator.is_enabled():
            return False
        return not await locator.evaluate("e => !!e.closest('.x-item-disabled,.x-btn-disabled,[aria-disabled=\"true\"],[disabled]')")

    @staticmethod
    def _offering_id(row):
        parts = [normalize(row.get(k, "")) for k in ("code", "class_name", "discipline", "year_text")]
        return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:20]

    async def discover(self):
        async with self._lock:
            await self._go_list()
            await self._page_control("first")
            first_frame, first_data = await self._wait_extract("offerings")
            first_text = normalize(await first_frame.locator("body").inner_text())
            first_ranges = re.findall(r"(\d+)\s*[-–]\s*(\d+)\s+DE\s+(\d+)", first_text)
            if any(int(start) > 1 for start, _, _ in first_ranges):
                raise PortalError("A lista está em uma página intermediária. Volte à primeira página ou configure o botão de primeira página para incluir todas as turmas.")
            found = {}
            seen = set()
            for page_number in range(1, int(self.config["max_offering_pages"]) + 1):
                frame, data = await self._wait_extract("offerings")
                signature = "|".join(self._offering_id(row) for row in data["rows"])
                if signature in seen:
                    raise PortalError("A paginação das turmas não avançou. Configure o botão de próxima página; a lista não foi considerada completa.")
                seen.add(signature)
                for row in data["rows"]:
                    years = re.findall(r"\b20\d{2}\b", row["year_text"])
                    if len(set(years)) != 1:
                        raise PortalError("Não foi possível identificar um único ano letivo na lista de turmas.")
                    if int(years[0]) != int(self.config["target_year"]):
                        continue
                    key = self._offering_id(row)
                    if key in found:
                        raise PortalError("Há turmas/disciplinas duplicadas com a mesma identificação. A lista precisa ser desambiguada antes do lançamento.")
                    row.update(id=key, year=int(years[0]), page_number=page_number)
                    found[key] = row
                # A visible count also protects against silently stopping after page 1.
                text = await frame.locator("body").inner_text()
                counts = re.findall(r"(?:EXIBINDO\s+REGISTROS?\s*)?(\d+)\s*[-–]\s*(\d+)\s+DE\s+(\d+)", normalize(text))
                more = any(int(end) < int(total) for _, end, total in counts)
                advanced = await self._page_control("next")
                if not advanced:
                    if more:
                        raise PortalError("Há mais turmas em outra página. Configure o botão de próxima página para incluir todas as disciplinas.")
                    break
                await asyncio.sleep(0.2)
            else:
                raise PortalError("A lista excedeu o limite de páginas configurado; nenhuma nota foi alterada.")
            if not found:
                raise PortalError("Nenhuma turma do ano letivo configurado foi localizada nesta conta.")
            self._offerings = list(found.values())
            return copy.deepcopy(self._offerings)

    async def _open_offering(self, offering, assessment=None):
        if int(offering.get("year", 0)) != int(self.config["target_year"]):
            raise PortalError("A turma não pertence ao ano letivo selecionado.")
        await self._go_list()
        # Always return to the first list page, then find by immutable row identity.
        await self._page_control("first")
        visited = set()
        for _ in range(int(self.config["max_offering_pages"])):
            frame, data = await self._wait_extract("offerings")
            signature = "|".join(self._offering_id(row) for row in data["rows"])
            if signature in visited:
                break
            visited.add(signature)
            rows = [row for row in data["rows"] if self._offering_id(row) == offering["id"]]
            if len(rows) == 1:
                row = rows[0]
                selector = row.get("link_selector") or row["row_selector"]
                await frame.locator(selector).click()
                await self._wait_extract("grades", assessment)
                await self._verify_metadata(offering)
                return
            if not await self._page_control("next"):
                break
            await asyncio.sleep(0.2)
        raise PortalError("A turma mudou ou saiu da lista. Localize as turmas novamente e gere uma nova conferência.")

    async def _verify_metadata(self, offering):
        if not self.config.get("strict_metadata", True):
            return
        texts = []
        for frame in self.page.frames:
            if not self._frame_allowed(frame.url):
                continue
            texts.append(await frame.evaluate("() => (document.body?.innerText||'')+' '+Array.from(document.querySelectorAll('input:not([type=password]):not([type=hidden]),select')).map(e=>e.value).join(' ')"))
        text = normalize(" ".join(texts))
        for key in ("class_name", "discipline", "year"):
            expected = normalize(offering[key])
            present = bool(re.search(r"(?<![A-Z0-9])" + re.escape(expected) + r"(?![A-Z0-9])", text))
            if not present:
                raise PortalError("A identificação da turma, disciplina ou ano não corresponde à seleção. Nenhuma nota foi alterada.")

    async def _collect(self, assessment=None):
        frame, initial = await self._wait_extract("grades", assessment)
        found = {}
        page_row_duplicates = set()
        def add(data):
            local = set()
            for raw in data["rows"]:
                ra = registration(raw["registration"])
                if not ra:
                    continue
                if ra in local:
                    page_row_duplicates.add(ra)
                local.add(ra)
                item = {**raw, "registration": ra, "grade": grade(raw["grade"])}
                if ra in found and (found[ra]["name"] != item["name"] or found[ra]["grade"] != item["grade"]):
                    raise PortalError("O mesmo código de aluno aparece com dados conflitantes no portal.")
                found[ra] = item
        add(initial)
        for scroller in initial["scrollers"]:
            loc = frame.locator(scroller["selector"])
            await loc.evaluate("e => e.scrollTop=0")
            for _ in range(int(self.config["max_scroll_steps"])):
                await asyncio.sleep(0.03)
                fresh_frame, data = await self._extract("grades", assessment)
                add(data)
                pos = await loc.evaluate("e=>({top:e.scrollTop,height:e.clientHeight,total:e.scrollHeight})")
                if pos["top"] + pos["height"] >= pos["total"] - 2:
                    break
                await loc.evaluate("e=>e.scrollTop+=Math.max(20,e.clientHeight*.8)")
            else:
                raise PortalError("A lista de alunos não terminou durante a leitura. A turma não foi considerada completa.")
            await loc.evaluate("e => e.scrollTop=0")
        if page_row_duplicates:
            raise PortalError("Há códigos de aluno duplicados na tabela do portal. Resolva a duplicidade antes de lançar.")
        if not found:
            raise PortalError("A turma não apresentou códigos de alunos para conferência.")
        body = normalize(await frame.locator("body").inner_text())
        ranges = re.findall(r"(\d+)\s*[-–]\s*(\d+)\s+DE\s+(\d+)", body)
        if any(int(total) > len(found) for _, _, total in ranges):
            raise PortalError("A tabela de alunos tem páginas adicionais que não foram lidas. Configure a visualização de todos os alunos antes de lançar.")
        return list(found.values())

    async def inspect_offering(self, offering):
        """Discover allowed assessment headers without opening any cell editor.

        Presence in the grid is a candidate, not a claim that the account has
        write permission. Calculated averages and C-* status columns are never
        returned. Inspection works even when the configured assessment is absent.
        """
        async with self._lock:
            await self._open_offering(offering, assessment="")
            _, data = await self._wait_extract("grades", assessment="")
            headers = [normalize(header) for header in data["headers"]]
            return {
                "class_id": offering["id"],
                "class_name": offering["class_name"],
                "discipline": offering["discipline"],
                "year": offering["year"],
                "headers": data["headers"],
                "assessments": [code for code in ASSESSMENT_CODES if headers.count(code) == 1],
                "writability": "candidate_from_header",
            }

    async def inspect_assessments(self, offering):
        return (await self.inspect_offering(offering))["assessments"]

    async def read_offering(self, offering, assessment=None):
        selected = self._assessment(assessment)
        async with self._lock:
            await self._open_offering(offering, selected)
            return await self._collect(selected)

    async def _find_cell(self, wanted_ra, assessment=None):
        frame, data = await self._extract("grades", assessment)
        def matches(rows):
            return [r for r in rows if registration(r["registration"]) == wanted_ra]
        candidates = matches(data["rows"])
        if len(candidates) == 1:
            return frame, candidates[0]
        for scroller in data["scrollers"]:
            loc = frame.locator(scroller["selector"])
            await loc.evaluate("e=>e.scrollTop=0")
            for _ in range(int(self.config["max_scroll_steps"])):
                await asyncio.sleep(0.03)
                frame, fresh = await self._extract("grades", assessment)
                candidates = matches(fresh["rows"])
                if len(candidates) == 1:
                    return frame, candidates[0]
                pos = await loc.evaluate("e=>({top:e.scrollTop,height:e.clientHeight,total:e.scrollHeight})")
                if pos["top"] + pos["height"] >= pos["total"] - 2:
                    break
                await loc.evaluate("e=>e.scrollTop+=Math.max(20,e.clientHeight*.8)")
        raise PortalError("O código do aluno não foi encontrado de forma única antes da edição.")

    async def _edit(self, frame, raw, new, assessment=None):
        cell = frame.locator(raw["cell_selector"])
        existing = cell.locator("input:not([type=hidden]):not([type=password]),textarea,[contenteditable=true]")
        if await existing.count() == 1 and await existing.is_visible():
            editor = existing
        else:
            ext_inner = cell.locator(".x-grid3-cell-inner")
            if await ext_inner.count() == 1:
                # The live Univap ExtJS grid starts editing on a single click.
                # Double clicking can commit/close its first editor instance.
                await ext_inner.click()
            else:
                await cell.dblclick()
            candidates = frame.locator(self.config["editor_selector"])
            try:
                await candidates.wait_for(state="visible", timeout=int(self.config["timeout_ms"]))
                visible_editor = await candidates.count() == 1
            except Exception:
                visible_editor = False
            if visible_editor:
                editor = candidates
            else:
                inside = cell.locator("input:not([type=hidden]):not([type=password]),textarea,[contenteditable=true]")
                if await inside.count() != 1 or not await inside.is_visible():
                    raise PortalError("O editor desta célula não foi reconhecido. A turma não foi salva; conclua a configuração inicial do editor.")
                editor = inside
        if not await editor.is_editable():
            raise PortalError("A avaliação está bloqueada para edição nesta conta ou turma.")
        await editor.fill(format(Decimal(str(new)), "f"))
        await editor.press("Tab")
        _, refreshed = await self._find_cell(registration(raw["registration"]), assessment)
        if not equal_grade(refreshed["grade"], new):
            raise PortalError("A célula não confirmou a nota digitada. A turma não foi salva.")

    async def _confirm_save_window(self):
        """Handle only the exact ExtJS confirmation supplied by the portal.

        The captured editorGridBase configuration uses this Save text. Other
        windows (including Undo and validation errors) are never accepted by
        clicking a generic Sim/OK button somewhere else on the page.
        """
        windows = []
        for frame in self.page.frames:
            if not self._frame_allowed(frame.url):
                continue
            candidates = frame.locator('.x-window:visible,[role="dialog"]:visible,[role="alertdialog"]:visible')
            for index in range(await candidates.count()):
                candidate = candidates.nth(index)
                # ExtJS can put a role on an inner wrapper. Count the outer
                # window once, never treat the same message as two dialogs.
                nested = await candidate.evaluate('e => !!e.parentElement?.closest(\'.x-window,[role="dialog"],[role="alertdialog"]\')')
                if not nested:
                    windows.append(candidate)
        if not windows:
            return False
        if len(windows) != 1 or self._save_confirmation_seen:
            self._unexpected_dialog = True
            raise PortalError("O portal exibiu confirmações inesperadas ao salvar. Nenhuma confirmação adicional foi aceita.")
        window = windows[0]
        message = window.locator(".ext-mb-text,.x-message-box-text")
        if await message.count() != 1:
            message = window.locator(".x-window-body")
        if await message.count() != 1 or normalize(await message.inner_text()) != normalize(SAVE_CONFIRMATION):
            self._unexpected_dialog = True
            raise PortalError("A mensagem de confirmação ao salvar não corresponde à tela validada. Confira o portal antes de repetir.")
        yes = []
        buttons = window.locator("button")
        for index in range(await buttons.count()):
            button = buttons.nth(index)
            if await button.is_visible() and await button.is_enabled() and normalize(await button.inner_text()) == "SIM":
                yes.append(button)
        if len(yes) != 1:
            self._unexpected_dialog = True
            raise PortalError("Não foi identificado um único botão Sim na confirmação de salvar. Nenhuma confirmação foi aceita.")
        self._save_confirmation_seen = True
        await yes[0].click()
        await window.wait_for(state="hidden", timeout=int(self.config["timeout_ms"]))
        return True

    async def _save(self):
        self._assert_dialog_safe()
        self._saving = True
        self._save_confirmation_seen = False
        try:
            explicit = self.config.get("save_selector")
            if explicit:
                _, loc = await self._unique_selector(explicit)
                await loc.click()
            else:
                await self._text_control("SALVAR")
            # A prompt can be rendered after an ExtJS AJAX response. Settle the
            # click first, confirm only its known message, then await the actual
            # save request before reloading the persisted view.
            await self.page.wait_for_load_state("networkidle", timeout=int(self.config["timeout_ms"]))
            self._assert_dialog_safe()
            await self._confirm_save_window()
            await self.page.wait_for_load_state("networkidle", timeout=int(self.config["timeout_ms"]))
            self._assert_dialog_safe()
            # A second/new modal cannot turn an uncertain Save into success.
            await self._confirm_save_window()
        finally:
            self._saving = False

    async def apply_offering(self, offering, changes, assessment=None):
        selected = self._assessment(assessment)
        async with self._lock:
            if not self.config.get("validated"):
                raise PortalError("Primeiro gere e revise a conferência das turmas e da coluna selecionada.")
            if not changes:
                return []
            if any(c.get("column") and normalize(c["column"]) != selected for c in changes):
                raise PortalError("O lote contém outra avaliação. Cada lançamento deve corresponder à coluna exata selecionada.")
            ids = [registration(c["registration"]) for c in changes]
            if not all(ids) or len(ids) != len(set(ids)):
                raise PortalError("Há códigos ausentes ou duplicados no lote; a turma não foi alterada.")
            # Validate every proposed grade before opening an editor.
            for change in changes:
                grade(change["new"])
                if change["new"] is None:
                    raise PortalError("Notas vazias não são lançadas automaticamente.")
            await self._open_offering(offering, selected)
            before = {row["registration"]: row for row in await self._collect(selected)}
            for change in changes:
                row = before.get(registration(change["registration"]))
                if not row or normalize(row["status"]) not in ("MATRICULADO", "ATIVO", "REGULAR"):
                    raise PortalError("Um aluno não está matriculado ou não consta mais na turma. Gere nova conferência.")
                if not equal_grade(row["grade"], change["old"]):
                    raise PortalError("Uma nota mudou no portal depois da conferência. Nenhuma alteração deste lote foi salva; gere nova conferência.")
            save_attempted = False
            try:
                total = len(changes)
                await self._report_progress({"phase": "fill", "completed": 0, "total": total, "assessment": selected})
                for index, change in enumerate(changes, 1):
                    frame, raw = await self._find_cell(registration(change["registration"]), selected)
                    if not equal_grade(raw["grade"], change["old"]):
                        raise PortalError("Uma nota mudou durante a preparação; a turma não foi salva.")
                    await self._edit(frame, raw, change["new"], selected)
                    await self._report_progress({"phase": "fill", "completed": index, "total": total, "assessment": selected})
                await self._report_progress({"phase": "save", "completed": 0, "total": 1, "assessment": selected})
                save_attempted = True
                await self._save()
                await self._report_progress({"phase": "save", "completed": 1, "total": 1, "assessment": selected})
                # A reload must fetch the persisted view; a changed input value
                # before reload is never counted as a successful launch.
                await self.page.reload(wait_until="domcontentloaded")
                await self._wait_extract("grades", selected)
                await self._verify_metadata(offering)
                after = {row["registration"]: row for row in await self._collect(selected)}
                results = []
                await self._report_progress({"phase": "verify", "completed": 0, "total": total, "assessment": selected})
                for index, change in enumerate(changes, 1):
                    row = after.get(registration(change["registration"]))
                    confirmed = bool(row and equal_grade(row["grade"], change["new"]))
                    results.append({**change, "status": "confirmed" if confirmed else "unknown", "message": "Nota confirmada após recarregar o portal." if confirmed else "Não foi possível confirmar a gravação. Confira no portal antes de repetir.", "verified": confirmed})
                    await self._report_progress({"phase": "verify", "completed": index, "total": total, "assessment": selected})
                return results
            except Exception as exc:
                # No automatic retry after Save; result may have been committed.
                if save_attempted:
                    return [{**c, "status": "unknown", "verified": False, "message": "Gravação tentada, mas sem confirmação após recarregar. Confira no portal antes de repetir."} for c in changes]
                try:
                    await self.page.reload(wait_until="domcontentloaded")
                except Exception:
                    pass
                if isinstance(exc, PortalError):
                    raise
                raise PortalError("A edição não foi concluída e a turma não foi salva. Confira a sessão no navegador.") from exc
