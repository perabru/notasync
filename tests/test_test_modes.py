"""API choices for quick, complete and explicitly skipped local tests.

All workbooks and accounts below are synthetic. Browser operations are mocked;
these tests never open the Univap portal or alter real grades.
"""
import asyncio
import copy
import importlib
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from test_app import imported, student

backend = importlib.import_module('notasync.app')
training = importlib.import_module('notasync.training')
CHECK_IDS = ('browser', 'login', 'classes', 'columns', 'fill', 'save', 'reload',
             'preservation', 'stale', 'cancelled', 'complete')


def synthetic_source():
    rows = []
    for code in backend.ASSESSMENTS:
        for index in range(1, 43):
            inactive = index == 42
            rows.append(student(ra=str(99010000 + index), value=None if inactive else (index % 11),
                                name='Aluno sintético ' + str(index), class_name='TESTE-1A',
                                assessment=code, discipline=code if code.startswith('AT') else 'Disciplina de teste',
                                portal_discipline='Disciplina de teste', status='Cancelado' if inactive else 'Matriculado',
                                eligible=not inactive, source_cell=code + ':' + str(index),
                                portal_values={'AR4': '2.0', 'MB4': '6.4', 'MBF': '6.4', 'EF': '', 'NC': ''}))
    return imported(rows, mode='AUTO', filename='notas_sinteticas.xlsx')


def successful_run(source_snapshot, assessments, test_mode):
    plan = training.get_training_plan(source_snapshot, assessments, test_mode)
    count = plan['planned_records']
    return {'passed': True, 'source_mode': 'imported_sheet', 'test_mode': test_mode,
            'session_id': 'synthetic-mode-session',
            'checks': [{'id': item, 'status': 'passed', 'label': item} for item in CHECK_IDS],
            'message': ('Teste resumido aprovado' if test_mode == 'quick' else 'Teste completo aprovado'),
            'counts': {**plan, 'tested_records': count, 'confirmed_writes': count,
                       'source_records': len(source_snapshot['rows']), 'students': 42, 'classes': 1}}


class TestModeChoices(unittest.TestCase):
    def setUp(self):
        self.original = dict(backend.state)
        config = copy.deepcopy(backend.DEFAULT_CONFIG)
        config.update(target_year=2026, discipline_aliases={})
        backend.state.clear()
        backend.state.update(imports=[synthetic_source()], portal={'status': 'disconnected', 'classes': []},
                             jobs=[], demo=False, config=config, plan=None, revision=0, audit=[],
                             adapter=None, task=None, _test_adapter=None,
                             validation={'status': 'idle', 'checks': [], 'test_id': None})

    def tearDown(self):
        backend.state.clear()
        backend.state.update(self.original)

    def post(self, client, path, body):
        return client.post(path, headers={'x-notasync-token': backend.TOKEN}, json=body)

    def current(self, client):
        return client.get('/api/state').json()['validation']

    def wait(self, client):
        async def wait_job():
            await asyncio.wait_for(asyncio.shield(backend.state['task']), timeout=3)
        client.portal.call(wait_job)
        return self.current(client)

    def skip(self, client, assessments=None):
        response = self.post(client, '/api/test/skip', {'assessments': list(assessments or backend.ASSESSMENTS)})
        self.assertEqual(response.status_code, 200, response.text)
        return self.current(client)

    def run_test(self, client, mode='quick', mutate=None, assessments=None, omit_mode=False):
        async def fake_run(base_url, selected, config, *, progress, source_snapshot, test_mode='full'):
            result = successful_run(source_snapshot, selected, test_mode)
            if mutate:
                mutate(result)
            return result
        runner = AsyncMock(side_effect=fake_run)
        body = {'assessments': list(assessments or backend.ASSESSMENTS)}
        if not omit_mode:
            body['mode'] = mode
        with patch.object(training, 'run_training', runner):
            response = self.post(client, '/api/test', body)
            self.assertEqual(response.status_code, 200, response.text)
            proof = self.wait(client)
        return proof, runner

    def assert_rejected_proof(self, identifier, assessments=None):
        with self.assertRaises(HTTPException) as error:
            backend.require_local_test(list(assessments or backend.ASSESSMENTS), identifier)
        self.assertEqual(error.exception.status_code, 409)

    def test_skip_records_explicit_choice_without_running_or_claiming_success(self):
        previous_browser = AsyncMock()
        backend.state['_test_adapter'] = previous_browser
        with patch.object(training, 'run_training', new_callable=AsyncMock) as runner, \
                patch.object(backend, 'PortalAdapter') as real_browser, TestClient(backend.app) as client:
            proof = self.skip(client)
            self.assertEqual(proof['status'], 'skipped')
            self.assertEqual(proof['test_mode'], 'skip')
            self.assertEqual(proof['source_mode'], 'imported_sheet')
            self.assertTrue(proof['test_id'])
            self.assertEqual(proof['fingerprint'], backend.test_fingerprint(backend.ASSESSMENTS))
            self.assertEqual(proof.get('checks', []), [])
            self.assertEqual(proof['summary']['tested_records'], 0)
            self.assertEqual(proof['summary']['confirmed_writes'], 0)
            self.assertEqual(proof['summary']['expected_records'], 328)
            self.assertNotIn('aprovado', proof['message'].lower())
            self.assertIsNone(backend.state['_test_adapter'])
            backend.require_local_test(list(backend.ASSESSMENTS), proof['test_id'])
            runner.assert_not_awaited()
            real_browser.assert_not_called()
            previous_browser.close.assert_awaited_once()

    def test_skip_permits_launch_without_a_hidden_test(self):
        with patch.object(training, 'run_training', new_callable=AsyncMock) as runner, TestClient(backend.app) as client:
            proof = self.skip(client)
            with patch.object(backend, 'PortalAdapter') as browser, \
                    patch.object(backend, 'start_job', return_value={'id': 'synthetic-launch', 'status': 'pending'}) as start:
                response = self.post(client, '/api/launch',
                                     {'username': 'conta_sintetica', 'password': 'segredo_sintetico',
                                      'assessments': list(backend.ASSESSMENTS), 'show_browser': False,
                                      'test_id': proof['test_id']})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(start.call_args.args[0], 'launch')
                browser.assert_not_called()  # The mocked job has not begun.
            runner.assert_not_awaited()

    def test_skip_rejects_invalid_selection_and_source_without_eligible_notes(self):
        with patch.object(training, 'run_training', new_callable=AsyncMock) as runner, TestClient(backend.app) as client:
            for selected in ([], ['AR4'], ['unknown'], 'AT4', [None], {}):
                with self.subTest(selected=selected):
                    response = self.post(client, '/api/test/skip', {'assessments': selected})
                    self.assertEqual(response.status_code, 400, response.text)
                    self.assertFalse(self.current(client).get('test_id'))
            backend.state['imports'] = [imported([student(value=None, eligible=False, status='Cancelado')])]
            response = self.post(client, '/api/test/skip', {'assessments': ['AT4']})
            self.assertEqual(response.status_code, 400, response.text)
            self.assertFalse(self.current(client).get('test_id'))
            runner.assert_not_awaited()

    def test_skip_is_bound_to_source_config_selection_and_identifier(self):
        original_imports = copy.deepcopy(backend.state['imports'])
        original_config = copy.deepcopy(backend.state['config'])
        with TestClient(backend.app) as client:
            for mutation in ('source', 'config', 'selection', 'identifier'):
                with self.subTest(mutation=mutation):
                    backend.state['imports'] = copy.deepcopy(original_imports)
                    backend.state['config'] = copy.deepcopy(original_config)
                    proof = self.skip(client)
                    identifier = proof['test_id']
                    selected = list(backend.ASSESSMENTS)
                    if mutation == 'source':
                        backend.state['imports'][0]['sheets'][0]['students'][0]['grade'] = 9.9
                    elif mutation == 'config':
                        backend.state['config']['target_year'] = 2027
                    elif mutation == 'selection':
                        selected = ['AT4']
                    else:
                        identifier = 'different-skip-ticket'
                    self.assert_rejected_proof(identifier, selected)

    def test_new_skip_ticket_replaces_an_earlier_ticket(self):
        with TestClient(backend.app) as client:
            first = self.skip(client)
            second = self.skip(client)
            self.assertNotEqual(first['test_id'], second['test_id'])
            self.assert_rejected_proof(first['test_id'])
            backend.require_local_test(list(backend.ASSESSMENTS), second['test_id'])

    def test_skip_does_not_label_demo_data_as_a_real_source(self):
        backend.state['demo'] = True
        with TestClient(backend.app) as client:
            proof = self.skip(client)
            self.assertEqual(proof['status'], 'skipped')
            self.assertEqual(proof['source_mode'], 'synthetic_demo')
            self.assertEqual(proof['summary']['tested_records'], 0)
            self.assertNotEqual(proof['status'], 'passed')

    def test_quick_approves_only_its_24_record_sample(self):
        with TestClient(backend.app) as client:
            proof, runner = self.run_test(client, 'quick')
            self.assertEqual(proof['status'], 'passed', proof)
            self.assertEqual(proof['test_mode'], 'quick')
            summary = proof['summary']
            self.assertEqual(summary['expected_records'], 328)
            self.assertEqual(summary['planned_records'], 24)
            self.assertEqual(summary['tested_records'], 24)
            self.assertEqual(summary['confirmed_writes'], 24)
            self.assertEqual(summary['sampled_out_records'], 304)
            self.assertEqual(summary['coverage_mode'], 'sample')
            self.assertIs(summary['coverage_complete'], False)
            self.assertIn('resumido', proof['message'].lower())
            self.assertNotIn('completo aprovado', proof['message'].lower())
            self.assertEqual(runner.await_args.kwargs['test_mode'], 'quick')
            self.assertEqual(runner.await_args.kwargs['source_snapshot']['expected_records'], 328)
            backend.require_local_test(list(backend.ASSESSMENTS), proof['test_id'])

    def test_full_approves_all_records_and_is_default_for_legacy_requests(self):
        with TestClient(backend.app) as client:
            proof, runner = self.run_test(client, omit_mode=True)
            self.assertEqual(proof['status'], 'passed', proof)
            self.assertEqual(proof['test_mode'], 'full')
            self.assertEqual(proof['summary']['tested_records'], 328)
            self.assertEqual(proof['summary']['confirmed_writes'], 328)
            self.assertEqual(proof['summary']['coverage_mode'], 'full')
            self.assertIs(proof['summary']['coverage_complete'], True)
            self.assertEqual(proof['summary']['sampled_out_records'], 0)
            self.assertEqual(runner.await_args.kwargs['test_mode'], 'full')
            backend.require_local_test(list(backend.ASSESSMENTS), proof['test_id'])

    def test_mode_and_sample_coverage_cannot_be_misreported(self):
        cases = (
            lambda r: r.update(test_mode='full'),
            lambda r: r.pop('test_mode'),
            lambda r: r['counts'].update(expected_records=24),
            lambda r: r['counts'].update(planned_records=328),
            lambda r: r['counts'].update(tested_records=23, confirmed_writes=23),
            lambda r: r['counts'].update(tested_records=24, confirmed_writes=23),
            lambda r: r['counts'].update(sampled_out_records=303),
            lambda r: r['counts'].update(coverage_complete=True),
            lambda r: r['counts'].update(coverage_mode='full'),
        )
        with TestClient(backend.app) as client:
            for number, mutation in enumerate(cases):
                with self.subTest(case=number):
                    proof, _ = self.run_test(client, 'quick', mutate=mutation)
                    self.assertEqual(proof['status'], 'failed', proof)
                    self.assertFalse(proof.get('test_id'))
                    self.assert_rejected_proof('incorrect-coverage-ticket')

    def test_invalid_test_modes_do_not_start_browser_jobs(self):
        with patch.object(training, 'run_training', new_callable=AsyncMock) as runner, TestClient(backend.app) as client:
            for mode in ('skip', 'fast', '', None, 3, []):
                with self.subTest(mode=mode):
                    response = self.post(client, '/api/test', {'assessments': ['AT4'], 'mode': mode})
                    self.assertEqual(response.status_code, 400, response.text)
            runner.assert_not_awaited()
            self.assertFalse(backend.state['jobs'])

    def test_full_test_supersedes_a_quick_or_skipped_choice(self):
        with TestClient(backend.app) as client:
            skipped = self.skip(client)
            quick, _ = self.run_test(client, 'quick')
            self.assertNotEqual(skipped['test_id'], quick['test_id'])
            self.assert_rejected_proof(skipped['test_id'])
            full, _ = self.run_test(client, 'full')
            self.assertEqual(full['status'], 'passed', full)
            self.assertNotEqual(quick['test_id'], full['test_id'])
            self.assert_rejected_proof(quick['test_id'])
            self.assertEqual(full['summary']['tested_records'], 328)
            backend.require_local_test(list(backend.ASSESSMENTS), full['test_id'])

    def test_mode_change_invalidation_revokes_every_kind_of_choice(self):
        with TestClient(backend.app) as client:
            for mode in ('skip', 'quick', 'full'):
                with self.subTest(mode=mode):
                    proof = self.skip(client) if mode == 'skip' else self.run_test(client, mode)[0]
                    response = self.post(client, '/api/test/invalidate', {'reason': 'Modo de teste alterado'})
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertFalse(self.current(client).get('test_id'))
                    self.assert_rejected_proof(proof['test_id'])


if __name__ == '__main__':
    unittest.main()
