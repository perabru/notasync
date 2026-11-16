"""Local training-portal boundaries and real-adapter browser verification.

The optional browser test needs only a loopback server and fictitious data.
It never connects to Univap or uses a real coordinator's credentials.
"""
import asyncio
import copy
import json
import os
from pathlib import Path
import socket
import threading
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from notasync import training
from notasync.portal import ASSESSMENT_CODES, PortalError


class TrainingRoutesTests(unittest.TestCase):
    def setUp(self):
        self.session = training.create_training_session(["AT4", "AT1"], 2026)
        app = FastAPI()
        app.include_router(training.router)
        self.client = TestClient(app)
        self.prefix = self.session.prefix

    def tearDown(self):
        self.client.close()
        training._sessions.pop(self.session.identifier, None)

    def login(self):
        response = self.client.post(self.prefix + "/login", json={"username": training.LOGIN, "password": training.PASSWORD})
        self.assertEqual(response.status_code, 200)
        return response

    def save(self, changes):
        return self.client.post(self.prefix + "/grades/redes/save", json={"changes": changes})

    def test_fictitious_login_and_cookie_are_scoped_to_one_training_session(self):
        response = self.client.get(self.prefix + "/login")
        self.assertIn("Ambiente de teste • Nenhuma nota real será alterada", response.text)
        self.assertIn("Não informe seu login ou senha da Univap", response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(self.client.get(self.prefix + "/classes").status_code, 401)
        self.assertEqual(self.client.post(self.prefix + "/login", json={"username": "coordenador.real", "password": "senha-real"}).status_code, 401)
        self.assertEqual(self.client.post(self.prefix + "/login", json=[]).status_code, 401)
        cookie = self.login().headers["set-cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=strict", cookie)
        self.assertIn("Path=" + self.prefix, cookie)
        other = training.create_training_session(["AT4"], 2026)
        try:
            self.assertEqual(self.client.get(other.prefix + "/classes").status_code, 401)
        finally:
            training._sessions.pop(other.identifier, None)

    def test_two_actual_pages_and_all_eight_exact_assessment_columns(self):
        self.login()
        first = self.client.get(self.prefix + "/classes").text
        second = self.client.get(self.prefix + "/classes?page=2").text
        self.assertIn("Redes de Computadores", first)
        self.assertNotIn("Banco de Dados", first)
        self.assertIn("Banco de Dados", second)
        self.assertNotIn("Redes de Computadores", second)
        self.assertIn("Exibindo registros 1 - 1 de 2", first)
        self.assertIn("Exibindo registros 2 - 2 de 2", second)
        grades = self.client.get(self.prefix + "/grades/redes").text
        for code in ASSESSMENT_CODES:
            self.assertIn("<th>" + code + "</th>", grades)
            self.assertIn("<th>C-" + code + "</th>", grades)
        self.assertIn("<th>MB4</th>", grades)
        self.assertIn("Aluno Teste Cancelado", grades)

    def test_save_persists_on_server_and_reloaded_html(self):
        self.login()
        before = copy.deepcopy(self.session.rosters)
        response = self.save([{"registration": "90000001", "assessment": "AT4", "value": 8.3},
                              {"registration": "90000001", "assessment": "AT1", "value": 0}])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["saved"], 2)
        before["redes"][0]["grades"].update(AT4=8.3, AT1=0.0)
        self.assertEqual(self.session.rosters, before)
        reloaded = self.client.get(self.prefix + "/grades/redes").text
        self.assertIn("<div class='x-grid3-cell-inner'>8.3</div>", reloaded)
        self.assertIn("<div class='x-grid3-cell-inner'>0.0</div>", reloaded)
        self.assertNotIn("localStorage", reloaded)
        self.assertEqual(self.session.save_count, 1)

    def test_rejects_invalid_batches_atomically_without_changing_any_grade(self):
        self.login()
        baseline = copy.deepcopy(self.session.rosters)
        valid = {"registration": "90000001", "assessment": "AT4", "value": 8.3}
        invalid = [
            {**valid, "registration": "90000003"},
            {**valid, "assessment": "C-AT4"},
            {**valid, "assessment": "MB4"},
            {**valid, "assessment": "A14"},
            {**valid, "registration": "missing"},
            {**valid, "registration": []},
            {**valid, "value": 11},
            {**valid, "value": -1},
            {**valid, "value": None},
            {**valid, "value": True},
            {**valid, "value": "8.3"},
        ]
        for bad in invalid:
            with self.subTest(change=bad):
                response = self.save([valid, bad])
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(self.session.rosters, baseline)
                self.assertEqual(self.session.save_count, 0)
        self.assertEqual(self.save([valid, valid]).status_code, 400)


class TrainingBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.session = training.create_training_session(["AT4", "AT1", "AT4"], 2026)

    def tearDown(self):
        training._sessions.pop(self.session.identifier, None)

    def test_assessments_have_canonical_order_and_strict_validation(self):
        self.assertEqual(self.session.assessments, ("AT1", "AT4"))
        for codes, year in (([], 2026), (["C-AT4"], 2026), (["MB4"], 2026), (["AT4"], True), (["AT4"], 1999)):
            with self.subTest(codes=codes, year=year), self.assertRaises(ValueError):
                training.create_training_session(codes, year)

    def test_exact_numeric_loopback_origin_and_nonce_path_only(self):
        adapter = training.TrainingPortalAdapter("http://127.0.0.1:8123", self.session, {"headless": True})
        allowed = "http://127.0.0.1:8123" + self.session.prefix
        adapter._check_target(allowed + "/classes?page=2")
        self.assertTrue(adapter._frame_allowed(allowed + "/login"))
        for url in ("https://portal.univap.br/", "http://127.0.0.1:8124" + self.session.prefix + "/login",
                    "http://127.0.0.1:8123/test-portal/other/login", allowed + "-extra/login",
                    "http://localhost:8123" + self.session.prefix + "/login", "about:blank",
                    allowed + "/../other/login", allowed + "/%2e%2e/other/login"):
            with self.subTest(url=url), self.assertRaises(PortalError):
                adapter._check_target(url)
            self.assertFalse(adapter._frame_allowed(url))
        self.assertEqual(adapter._launch_options(), {"headless": False, "slow_mo": 8})
        self.assertFalse(adapter.config["headless"])
        for base in ("https://127.0.0.1:8123", "http://example.org:8123", "http://localhost:8123",
                     "http://192.168.1.1:8123", "http://127.0.0.1:8123/extra", "http://127.0.0.1"):
            with self.subTest(base=base), self.assertRaises(PortalError):
                training.TrainingPortalAdapter(base, self.session)


class TrainingFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_browser_failure_does_not_return_a_pass(self):
        class BrokenBrowser:
            def __init__(self, *args):
                pass
            async def open(self):
                raise PortalError("Navegador indisponível no computador de teste.")
        events = []
        with patch.object(training, "TrainingPortalAdapter", BrokenBrowser):
            result = await training.run_training("http://127.0.0.1:8123", ["AT4"], {"target_year": 2026}, events.append)
        self.assertFalse(result["passed"])
        self.assertTrue(any(item["status"] == "failed" for item in result["checks"]))
        self.assertEqual(result["checks"][-1]["id"], "complete")
        self.assertNotIn("confirmed_writes", result)
        self.assertEqual(events[0]["status"], "running")
        training._sessions.pop(result["session_id"], None)

    async def test_unconfirmed_save_never_unlocks_real_execution(self):
        class UnconfirmedBrowser:
            def __init__(self, base_url, session, config):
                self.session = session
            async def open(self):
                return {"status": "opened"}
            async def discover(self):
                return self.session.offerings
            async def inspect_offering(self, offering):
                return {"assessments": ["AT4"]}
            async def read_offering(self, offering, assessment):
                return [{**row, "grade": row["grades"][assessment]} for row in self.session.rosters[offering["key"]]]
            async def apply_offering(self, offering, changes, assessment):
                return [{**changes[0], "status": "unknown", "verified": False}]
        events=[]
        with patch.object(training, "TrainingPortalAdapter", UnconfirmedBrowser):
            result = await training.run_training("http://127.0.0.1:8123", ["AT4"], {"target_year": 2026}, progress=events.append)
        self.assertFalse(result["passed"])
        self.assertIn("não confirmou", result["message"])
        self.assertNotIn("preservation", {item["id"] for item in result["checks"]})
        self.assertFalse(any(item.get('progress',{}).get('percent')==100 for item in events))
        training._sessions.pop(result["session_id"], None)

    async def test_display_callback_error_does_not_replace_the_real_browser_failure(self):
        class BrokenBrowser:
            def __init__(self,*args):pass
            async def open(self):raise PortalError('Falha real do navegador de teste.')
        async def broken_display(item):
            raise RuntimeError('Falha somente do mostrador.')
        with patch.object(training,'TrainingPortalAdapter',BrokenBrowser):
            result=await training.run_training('http://127.0.0.1:8123',['AT4'],{'target_year':2026},progress=broken_display)
        self.addCleanup(training._sessions.pop,result['session_id'],None)
        self.assertFalse(result['passed'])
        self.assertEqual(result['message'],'Falha real do navegador de teste.')
        self.assertNotIn('mostrador',result['message'])

    async def test_adapter_confirmation_without_persisted_server_values_does_not_pass(self):
        class MisreportingBrowser:
            def __init__(self, base_url, session, config):
                self.session = session
            async def open(self):
                return {"status": "opened"}
            async def discover(self):
                return self.session.offerings
            async def inspect_offering(self, offering):
                return {"assessments": ["AT4"]}
            async def read_offering(self, offering, assessment):
                return [{**row, "grade": row["grades"][assessment]} for row in self.session.rosters[offering["key"]]]
            async def apply_offering(self, offering, changes, assessment):
                # A fabricated browser confirmation must not override server evidence.
                return [{**changes[0], "status": "confirmed", "verified": True}]
        with patch.object(training, "TrainingPortalAdapter", MisreportingBrowser):
            result = await training.run_training("http://127.0.0.1:8123", ["AT4"], {"target_year": 2026})
        self.assertFalse(result["passed"])
        self.assertEqual(result["checks"][-1]["status"], "failed")
        self.assertNotIn("preservation", {item["id"] for item in result["checks"]})
        training._sessions.pop(result["session_id"], None)


class ImportedSnapshotTests(unittest.TestCase):
    def snapshot(self):
        rows=[]
        for index in range(42):
            status='Cancelado' if index == 41 else 'Matriculado'
            values={code: str((index + n) % 11) for n,code in enumerate(ASSESSMENT_CODES)}
            values.update({'AR1':'2.5','MB1':'6.4','MBF':'7.2','EF':'','NC':'','CURSO':'111'})
            for code in ASSESSMENT_CODES:
                rows.append({'registration':str(97000000+index),'name':'Pessoa sintética '+str(index+1),
                             'class_name':'TESTE-1A','source_discipline':'Redes de Computadores',
                             'discipline': code if code.startswith('AT') else 'Redes de Computadores',
                             'assessment':code,'grade':None if status=='Cancelado' else values[code],
                             'status':status,'eligible':status=='Matriculado','portal_values':values,
                             'course':'111','source_file':'origem-sintetica.xlsx','issues':[]})
        return {'rows':rows,'metadata':[{'class_name':'TESTE-1A','discipline_name':'Redes de Computadores',
                     'discipline_code':'TESTE01','course':'Técnico em Informática','year':2026,'period':'0'}]}

    def test_every_imported_selected_eligible_value_is_targeted_without_sampling(self):
        original=self.snapshot()
        before=copy.deepcopy(original)
        session=training.create_training_session(list(ASSESSMENT_CODES),2026,original)
        self.addCleanup(training._sessions.pop,session.identifier,None)
        self.assertEqual(session.source_mode,'imported_sheet')
        self.assertEqual(session.source_students,42)
        self.assertEqual(session.source_records,336)
        self.assertEqual(len(session.offerings),1)
        key=session.offerings[0]['key']
        self.assertEqual(sum(len(values) for values in session.targets[key].values()),328)
        self.assertEqual(len(session.rosters[key]),43) # 42 source students plus unchanged control
        for row in session.rosters[key]:
            for code,targets in session.targets[key].items():
                if row['registration'] in targets:
                    self.assertNotEqual(row['grades'][code],targets[row['registration']])
            if row['registration']=='97000041':
                self.assertEqual(row['status'],'Cancelado')
                self.assertFalse(any(row['registration'] in targets for targets in session.targets[key].values()))
        self.assertEqual(original,before)
        self.assertEqual(session.rosters[key][0]['readonly']['MB1'],'6.4')
        self.assertEqual(session.rosters[key][0]['readonly']['AR1'],'2.5')

    def test_subset_preserves_original_nonselected_grade_and_readonly_columns(self):
        session=training.create_training_session(['AT4'],2026,self.snapshot())
        self.addCleanup(training._sessions.pop,session.identifier,None)
        key=session.offerings[0]['key']
        self.assertEqual(set(session.targets[key]),{'AT4'})
        self.assertEqual(len(session.targets[key]['AT4']),41)
        self.assertEqual(session.rosters[key][0]['grades']['A11'],0.0)
        self.assertEqual(session.rosters[key][0]['readonly']['MBF'],'7.2')
        self.assertEqual(session.source_records,42)

    def test_source_groups_do_not_mix_distinct_subjects_or_classes(self):
        snapshot=self.snapshot()
        row=copy.deepcopy(snapshot['rows'][0])
        row.update(source_discipline='Banco de Dados',grade=8.4)
        snapshot['rows'].append(row)
        session=training.create_training_session(list(ASSESSMENT_CODES),2026,snapshot)
        self.addCleanup(training._sessions.pop,session.identifier,None)
        self.assertEqual(len(session.offerings),2)
        self.assertEqual(sum(len(values) for by_code in session.targets.values() for values in by_code.values()),329)

    def test_conflicting_duplicate_is_rejected_and_blocked_sources_never_create_targets(self):
        snapshot=self.snapshot()
        duplicate=copy.deepcopy(snapshot['rows'][0]);duplicate['grade']=9.9
        snapshot['rows'].append(duplicate)
        with self.assertRaisesRegex(ValueError,'conflitantes'):
            training.create_training_session(list(ASSESSMENT_CODES),2026,snapshot)
        with self.assertRaisesRegex(ValueError,'aptas'):
            training.create_training_session(['AT4'],2026,{'rows':[{'registration':'90000001','assessment':'AT4','grade':8,'eligible':False}]})

    def test_replica_contains_real_sections_grid_and_expected_confirmation(self):
        session=training.create_training_session(list(ASSESSMENT_CODES),2026,self.snapshot())
        self.addCleanup(training._sessions.pop,session.identifier,None)
        app=FastAPI();app.include_router(training.router)
        with TestClient(app) as client:
            client.post(session.prefix+'/login',json={'username':training.LOGIN,'password':training.PASSWORD})
            html=client.get(session.prefix+'/grades/'+session.offerings[0]['key']).text
        for fragment in ['Selecionar uma Turma','Critérios de Avaliação','Selecionar avaliações a serem exibidas',
                         'Lançamento de notas:',"id='gridNotas'",'height:300px','Deseja salvar as alterações efetuadas?',
                         'Deseja desfazer as alterações efetuadas?','x-window x-window-dlg','Cópia local da planilha importada']:
            self.assertIn(fragment,html)
        for column in ['Aluno','Código','Situação','Curso','Média Final','AR1','MB1','AR4','MB4','MBF','EF','NC']:
            self.assertIn('<th>'+column+'</th>',html)
        self.assertNotIn('portal.univap.br',html)
        self.assertNotIn('https://',html)


class TrainingModeTests(unittest.TestCase):
    def snapshot(self):
        return ImportedSnapshotTests().snapshot()

    def session(self, snapshot=None, codes=ASSESSMENT_CODES, mode='quick'):
        result=training.create_training_session(list(codes),2026,snapshot or self.snapshot(),test_mode=mode)
        self.addCleanup(training._sessions.pop,result.identifier,None)
        return result

    def test_quick_plan_reports_sample_and_keeps_total_coverage_honest(self):
        snapshot=self.snapshot()
        before=copy.deepcopy(snapshot)
        sessions=set(training._sessions)
        plan=training.get_training_plan(snapshot,list(ASSESSMENT_CODES),'quick')
        self.assertEqual(plan,{'test_mode':'quick','expected_records':328,'planned_records':24,
                               'sampled_out_records':304,'coverage_mode':'sample','coverage_complete':False})
        self.assertEqual(set(training._sessions),sessions)
        self.assertEqual(snapshot,before)

    def test_full_mode_is_integral_and_remains_the_default(self):
        snapshot=self.snapshot()
        plan=training.get_training_plan(snapshot,list(ASSESSMENT_CODES))
        self.assertEqual(plan['test_mode'],'full')
        self.assertEqual(plan['expected_records'],328)
        self.assertEqual(plan['planned_records'],328)
        self.assertEqual(plan['sampled_out_records'],0)
        self.assertTrue(plan['coverage_complete'])
        self.assertEqual(plan['coverage_mode'],'full')
        session=self.session(mode='full')
        self.assertEqual(sum(len(targets) for values in session.targets.values() for targets in values.values()),328)

    def test_quick_sample_spans_first_middle_last_and_is_independent_of_source_order(self):
        original=self.snapshot()
        reverse=copy.deepcopy(original);reverse['rows'].reverse()
        first=self.session(original)
        second=self.session(reverse)
        expected={'97000000','97000020','97000040'}
        for session in (first,second):
            for by_code in session.targets.values():
                for targets in by_code.values():
                    self.assertEqual(set(targets),expected)
        self.assertEqual(first.expected_records,328)
        self.assertEqual(first.planned_records,24)

    def test_unsampled_values_cancelled_students_and_readonly_columns_are_preserved(self):
        snapshot=self.snapshot()
        before=copy.deepcopy(snapshot)
        session=self.session(snapshot)
        key=session.offerings[0]['key']
        by_ra={row['registration']:row for row in session.rosters[key]}
        unchanged=by_ra['97000001']
        original=next(row for row in snapshot['rows'] if row['registration']=='97000001')
        self.assertEqual(unchanged['grades'],{code:float(original['portal_values'][code]) for code in ASSESSMENT_CODES})
        self.assertEqual(unchanged['readonly']['MBF'],'7.2')
        self.assertEqual(unchanged['readonly']['AR1'],'2.5')
        self.assertEqual(by_ra['97000041']['status'],'Cancelado')
        self.assertFalse(any('97000041' in targets for targets in session.targets[key].values()))
        self.assertEqual(snapshot,before)

    def test_sample_is_per_class_subject_and_assessment_not_one_global_limit(self):
        snapshot=self.snapshot()
        extra=copy.deepcopy(snapshot['rows'])
        for row in extra:
            row['class_name']='TESTE-2B'
            row['source_discipline']='Banco de Dados'
        snapshot['rows']+=extra
        plan=training.get_training_plan(snapshot,['A11','AT4'],'quick')
        self.assertEqual(plan['expected_records'],164)
        self.assertEqual(plan['planned_records'],12)
        self.assertEqual(plan['sampled_out_records'],152)

    def test_small_quick_source_reports_actual_complete_coverage_without_claiming_full_mode(self):
        snapshot=self.snapshot()
        snapshot['rows']=[row for row in snapshot['rows'] if row['registration'] in ('97000000','97000001')]
        plan=training.get_training_plan(snapshot,['AT4'],'quick')
        self.assertEqual(plan['test_mode'],'quick')
        self.assertEqual(plan['expected_records'],2)
        self.assertEqual(plan['planned_records'],2)
        self.assertEqual(plan['sampled_out_records'],0)
        self.assertTrue(plan['coverage_complete'])

    def test_skip_and_unknown_modes_cannot_silently_open_a_training_browser(self):
        for mode in ('skip','resumido','FULL','',None,True):
            with self.subTest(mode=mode),self.assertRaises(ValueError):
                training.get_training_plan(self.snapshot(),['AT4'],mode)
            with self.subTest(mode=mode),self.assertRaises(ValueError):
                training.create_training_session(['AT4'],2026,self.snapshot(),test_mode=mode)


class QuickPreservationTests(unittest.IsolatedAsyncioTestCase):
    async def test_unsampled_mutation_cannot_receive_a_quick_pass(self):
        class UnexpectedMutationBrowser:
            def __init__(self,base_url,session,config):
                self.session=session
            async def open(self):
                return {'status':'opened'}
            async def discover(self):
                return self.session.offerings
            async def inspect_offering(self,offering):
                return {'assessments':['AT4']}
            async def read_offering(self,offering,assessment):
                return [{**row,'grade':row['grades'][assessment]} for row in self.session.rosters[offering['key']]]
            async def apply_offering(self,offering,changes,assessment):
                rows=self.session.rosters[offering['key']]
                for change in changes:
                    next(row for row in rows if row['registration']==change['registration'])['grades'][assessment]=change['new']
                next(row for row in rows if row['registration']=='97000001')['grades'][assessment]=9.9
                self.session.save_count+=1;self.session.confirmations+=1
                return [{**change,'status':'confirmed','verified':True} for change in changes]
        with patch.object(training,'TrainingPortalAdapter',UnexpectedMutationBrowser):
            result=await training.run_training('http://127.0.0.1:8123',['AT4'],{'target_year':2026},
                                               source_snapshot=ImportedSnapshotTests().snapshot(),test_mode='quick')
        self.addCleanup(training._sessions.pop,result['session_id'],None)
        self.assertFalse(result['passed'])
        self.assertEqual(result['test_mode'],'quick')
        self.assertIn('fora do lote',result['message'])
        self.assertFalse(any(check['id']=='preservation' for check in result['checks']))


@unittest.skipUnless(os.environ.get("NOTASYNC_TRAINING_BROWSER_TESTS") == "1", "Use NOTASYNC_TRAINING_BROWSER_TESTS=1 para abrir um navegador local de QA.")
class TrainingBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_quick_browser_tests_24_of_328_and_preserves_unsampled_rows(self):
        import uvicorn
        from notasync import app as backend

        sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen(128)
        base='http://127.0.0.1:'+str(sock.getsockname()[1])
        server=uvicorn.Server(uvicorn.Config(backend.app,log_level='error',lifespan='off',access_log=False))
        thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True)
        thread.start()
        result=None
        snapshot=ImportedSnapshotTests().snapshot()
        original=copy.deepcopy(snapshot)
        events=[]
        async def observe(item):
            events.append(copy.deepcopy(item))
            value=item.get('progress',{})
            if value.get('phase')=='fill' and value.get('filled')==5:
                raise RuntimeError('Um erro de exibição não pode afetar a gravação.')
        try:
            for _ in range(200):
                if server.started:break
                await asyncio.sleep(.01)
            self.assertTrue(server.started)
            options={'headless':True}
            if os.environ.get('NOTASYNC_TEST_BROWSER'):
                options['executable_path']=os.environ['NOTASYNC_TEST_BROWSER']
            with patch.object(training.TrainingPortalAdapter,'_launch_options',return_value=options):
                result=await training.run_training(base,list(ASSESSMENT_CODES),
                    {'target_year':2026,'browser_channel':'','timeout_ms':6000},progress=observe,source_snapshot=snapshot,test_mode='quick')
            public={key:value for key,value in result.items() if key!='adapter'}
            evidence=os.environ.get('NOTASYNC_TRAINING_EVIDENCE')
            if evidence:
                output=Path(evidence);output.mkdir(parents=True,exist_ok=True)
                (output/'quick-training-browser-result.json').write_text(json.dumps({**public,'events':events},ensure_ascii=False,indent=2),encoding='utf-8')
            self.assertTrue(result['passed'],json.dumps(public,ensure_ascii=False))
            self.assertEqual(result['test_mode'],'quick')
            counts=result['counts']
            self.assertEqual(counts['source_records'],336)
            self.assertEqual(counts['expected_records'],328)
            self.assertEqual(counts['tested_records'],24)
            self.assertEqual(counts['confirmed_writes'],24)
            self.assertEqual(counts['sampled_out_records'],304)
            self.assertEqual(counts['blocked_records'],8)
            self.assertEqual(counts['save_batches'],8)
            self.assertFalse(counts['coverage_complete'])
            self.assertIn('Teste resumido aprovado',result['message'])
            self.assertIn('24 de 328',result['message'])
            self.assertTrue(all(check['status']=='passed' for check in result['checks']))
            self.assertEqual(len(result['checks']),11)
            complete=next(check for check in result['checks'] if check['id']=='complete')
            self.assertNotIn('sem amostragem',complete['detail'])
            session=training._sessions[result['session_id']]
            key=session.offerings[0]['key']
            by_ra={row['registration']:row for row in session.rosters[key]}
            for code in ASSESSMENT_CODES:
                for ra,target in session.targets[key][code].items():
                    self.assertEqual(by_ra[ra]['grades'][code],target)
            untouched=next(row for row in original['rows'] if row['registration']=='97000001')
            self.assertEqual(by_ra['97000001']['grades'],{code:float(untouched['portal_values'][code]) for code in ASSESSMENT_CODES})
            self.assertEqual(by_ra['97000001']['readonly']['AR1'],'2.5')
            self.assertEqual(by_ra['97000001']['readonly']['MBF'],'7.2')
            self.assertEqual(snapshot,original)
            progress_events=[item for item in events if isinstance(item.get('progress'),dict)]
            values=[item['progress'] for item in progress_events]
            self.assertTrue(values)
            self.assertEqual(values[0]['phase'],'opening')
            self.assertEqual(values[0]['percent'],0)
            self.assertTrue(all(value['total']==24 and value['unit']=='notas' for value in values))
            self.assertTrue(all(value['completed']==value['verified'] for value in values))
            self.assertEqual({value['filled'] for value in values if value['phase']=='fill'},set(range(25)))
            self.assertEqual({value['verified'] for value in values if value['phase']=='verify'},set(range(25)))
            self.assertEqual([value['percent'] for value in values],sorted(value['percent'] for value in values))
            self.assertEqual(values[-1],{'phase':'complete','completed':24,'filled':24,'verified':24,'total':24,
                                        'percent':100,'unit':'notas','message':'Teste concluído: 24 de 24 notas conferidas.'})
            self.assertTrue(all(item['id']=='complete' and item['status']=='passed' for item in progress_events if item['progress']['percent']==100))
            for value in values:
                self.assertEqual(set(value),{'phase','completed','filled','verified','total','percent','unit','message'})
            self.assertNotIn('970000',json.dumps(values))
        finally:
            if result and result.get('adapter'):
                await result['adapter'].close()
                training._sessions.pop(result['session_id'],None)
            server.should_exit=True
            await asyncio.to_thread(thread.join,3)
            sock.close()

    async def test_real_browser_login_pagination_all_columns_save_reload_and_guards(self):
        import uvicorn
        from notasync import app as backend

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        base = "http://127.0.0.1:" + str(sock.getsockname()[1])
        server = uvicorn.Server(uvicorn.Config(backend.app, log_level="error", lifespan="off", access_log=False))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        result = None
        events = []
        try:
            for _ in range(200):
                if server.started:
                    break
                await asyncio.sleep(.01)
            self.assertTrue(server.started)
            options = {"headless": True}
            if os.environ.get("NOTASYNC_TEST_BROWSER"):
                options["executable_path"] = os.environ["NOTASYNC_TEST_BROWSER"]
            with patch.object(training.TrainingPortalAdapter, "_launch_options", return_value=options):
                result = await training.run_training(base, list(ASSESSMENT_CODES), {"target_year": 2026, "browser_channel": "", "timeout_ms": 6000}, events.append)
            evidence = os.environ.get("NOTASYNC_TRAINING_EVIDENCE")
            if evidence:
                output = Path(evidence)
                output.mkdir(parents=True, exist_ok=True)
                public = {key: value for key, value in result.items() if key != "adapter"}
                (output / "training-browser-result.json").write_text(json.dumps({**public, "events": events}, ensure_ascii=False, indent=2), encoding="utf-8")
                if result.get("adapter") and result["adapter"].page:
                    await result["adapter"].page.screenshot(path=str(output / "training-portal-browser.png"), full_page=True)
            self.assertTrue(result["passed"], json.dumps({key: value for key, value in result.items() if key != "adapter"}, ensure_ascii=False))
            self.assertEqual(result["confirmed_writes"], 16)
            required = {"browser", "login", "classes", "columns", "fill", "save", "reload", "preservation", "stale", "cancelled", "complete"}
            self.assertEqual({check["id"] for check in result["checks"]}, required)
            self.assertTrue(all(check["status"] == "passed" for check in result["checks"]))
            session = training._sessions[result["session_id"]]
            for rows in session.rosters.values():
                self.assertEqual(rows[0]["grades"], training.TARGETS)
                self.assertTrue(all(value == 7.0 for value in rows[1]["grades"].values()))
                self.assertTrue(all(value is None for value in rows[2]["grades"].values()))
        finally:
            if result and result.get("adapter"):
                await result["adapter"].close()
                training._sessions.pop(result["session_id"], None)
            server.should_exit = True
            await asyncio.to_thread(thread.join, 3)
            sock.close()


if __name__ == "__main__":
    unittest.main()
