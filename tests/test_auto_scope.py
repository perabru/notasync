"""Automatic matching and account-scope regressions using fictitious students."""
import copy
import importlib
import unittest
from unittest.mock import AsyncMock

backend = importlib.import_module('notasync.app')


def source(mode='A14', value=8.2, *, subject='Redes de Computadores',
           class_name='EConInf-1PMA', year=2026, period='0',
           source_format='portal_export', ra='50000001', status='Matriculado', filename='notas.xlsx'):
    row = {'registration':ra, 'name':'Aluno de Teste', 'class_name':class_name,
           'discipline':mode if mode.startswith('AT') else subject,
           'portal_discipline':subject, 'assessment':mode, 'grade':value,
           'year':year, 'period':period, 'source_format':source_format,
           'status':status, 'issues':[], 'eligible':value is not None and status=='Matriculado',
           'source_cell':'E10'}
    metadata = {'class_name':class_name, 'discipline_name':subject, 'year':year, 'period':period}
    return {'id':filename, 'filename':filename, 'mode':'AUTO',
            'sheets':[{'sheet':'Notas', 'class_name':class_name, 'discipline':row['discipline'],
                       'source_format':source_format, 'portal_metadata':metadata,
                       'metadata':metadata, 'students':[row], 'issues':[]}], 'issues':[]}


def offering(identifier='redes', *, subject='Redes de Computadores',
             class_name='EConInf-1PMA', year='Ano 2026', period='0'):
    return {'id':identifier, 'discipline':subject, 'class_name':class_name, 'year':year, 'period':period}


class AutomaticScopeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original = dict(backend.state)
        self.adapter = AsyncMock()
        self.adapter.config = {}
        self.rosters = {}
        self.read_ids = []
        async def read(item):
            self.read_ids.append(item['id'])
            if item['id'] not in self.rosters:
                raise AssertionError('A conta não disponibilizou esta turma.')
            return copy.deepcopy(self.rosters[item['id']])
        self.adapter.read_offering.side_effect = read
        backend.state.clear()
        backend.state.update(imports=[], portal={'status':'connected', 'classes':[]},
                             config={'target_year':2026, 'discipline_aliases':{}},
                             demo=False, adapter=self.adapter, revision=0, plan=None,
                             jobs=[], audit=[], task=None, _test_adapter=None,
                             validation={'status':'idle', 'checks':[]})

    async def asyncTearDown(self):
        self.adapter.apply_offering.assert_not_awaited()
        backend.state.clear()
        backend.state.update(self.original)

    def set_account(self, offerings, *, target_status='Matriculado', ra='50000001'):
        backend.state['portal']['classes'] = copy.deepcopy(offerings)
        self.rosters = {item['id']:[{'registration':ra, 'name':'Aluno de Teste',
                                    'status':target_status, 'grade':4.0}] for item in offerings}

    async def compare(self, mode='A14', ids=None):
        return await backend.build_plan(ids or [], mode, {})

    async def test_teacher_only_reads_offerings_accessible_to_own_account(self):
        teacher = offering('teacher-redes')
        self.set_account([teacher])
        backend.state['imports'] = [source(), source(subject='Banco de Dados', filename='banco.xlsx')]
        plan = await self.compare()
        self.assertEqual(self.read_ids, ['teacher-redes'])
        self.assertEqual([row['class_id'] for row in plan['changes']], ['teacher-redes'])
        self.assertTrue(any(row.get('discipline')=='Banco de Dados' for row in plan['blocked']))

    async def test_forged_class_id_cannot_add_an_unavailable_offering(self):
        self.set_account([offering('teacher-redes')])
        backend.state['imports'] = [source()]
        with self.assertRaises(ValueError):
            await self.compare(ids=['not-in-this-account'])
        self.assertEqual(self.read_ids, [])

    async def test_teacher_with_other_subject_does_not_receive_exam_grade(self):
        self.set_account([offering(subject='Banco de Dados')])
        backend.state['imports'] = [source(subject='Redes de Computadores')]
        plan = await self.compare()
        self.assertEqual(plan['changes'], [])
        self.assertTrue(plan['blocked'])

    async def test_explicit_source_year_mismatch_blocks_exam_and_project(self):
        self.set_account([offering()])
        for mode in ('A14', 'AT4'):
            with self.subTest(mode=mode):
                backend.state['imports'] = [source(mode, year=2025)]
                plan = await self.compare(mode)
                self.assertEqual(plan['changes'], [])
                self.assertTrue(plan['blocked'])

    async def test_complete_export_class_mismatch_blocks_exam_and_project(self):
        self.set_account([offering(class_name='EConInf-1PMB')])
        for mode in ('A14', 'AT4'):
            with self.subTest(mode=mode):
                backend.state['imports'] = [source(mode, class_name='EConInf-1PMA')]
                plan = await self.compare(mode)
                self.assertEqual(plan['changes'], [])
                self.assertTrue(plan['blocked'])

    async def test_explicit_period_mismatch_cannot_write_same_ra_in_other_period(self):
        self.set_account([offering(period='2')])
        backend.state['imports'] = [source(period='1')]
        plan = await self.compare()
        self.assertEqual(plan['changes'], [])
        self.assertTrue(plan['blocked'])

    async def test_semester_export_is_blocked_when_offering_does_not_confirm_period(self):
        item = offering()
        item.pop('period')
        self.set_account([item])
        backend.state['imports'] = [source(period='1')]
        plan = await self.compare()
        self.assertEqual(plan['changes'], [])
        self.assertTrue(plan['blocked'])

    async def test_annual_export_matches_actual_portal_year_without_extra_period_field(self):
        item = offering()
        item.pop('period')
        self.set_account([item])
        backend.state['imports'] = [source(period='0')]
        plan = await self.compare()
        self.assertEqual(len(plan['changes']), 1)

    async def test_distinct_source_years_do_not_merge_grades_for_same_registration(self):
        self.set_account([offering()])
        backend.state['imports'] = [source(value=2.3, year=2025, filename='anterior.xlsx'),
                                    source(value=8.1, year=2026, filename='atual.xlsx')]
        plan = await self.compare()
        self.assertEqual(len(plan['changes']), 1)
        self.assertEqual(plan['changes'][0]['new'], 8.1)
        self.assertEqual(plan['changes'][0]['source_file'], 'atual.xlsx')

    async def test_distinct_explicit_class_scopes_choose_correct_grade_not_global_conflict(self):
        self.set_account([offering(class_name='EConInf-1PMB')])
        backend.state['imports'] = [source(value=3.1, class_name='EConInf-1PMA', filename='a.xlsx'),
                                    source(value=8.4, class_name='EConInf-1PMB', filename='b.xlsx')]
        plan = await self.compare()
        self.assertEqual(len(plan['changes']), 1)
        self.assertEqual(plan['changes'][0]['new'], 8.4)
        self.assertEqual(plan['changes'][0]['source_file'], 'b.xlsx')

    async def test_project_export_with_subject_only_plans_that_subject(self):
        for mode in ('AT1', 'AT2', 'AT3', 'AT4'):
            with self.subTest(mode=mode):
                self.set_account([offering('redes'), offering('banco', subject='Banco de Dados')])
                backend.state['imports'] = [source(mode, subject='Redes de Computadores')]
                plan = await self.compare(mode)
                self.assertEqual([row['class_id'] for row in plan['changes']], ['redes'])
                self.assertEqual(plan['changes'][0]['column'], mode)
                self.assertEqual(plan['changes'][0]['new'], 8.2)

    async def test_distinct_project_subjects_keep_independent_grades_for_same_registration(self):
        for mode in ('AT1', 'AT2', 'AT3', 'AT4'):
            with self.subTest(mode=mode):
                self.set_account([offering('redes'), offering('banco', subject='Banco de Dados')])
                backend.state['imports'] = [source(mode, value=3.1, subject='Redes de Computadores', filename='redes.xlsx'),
                                            source(mode, value=8.4, subject='Banco de Dados', filename='banco.xlsx')]
                plan = await self.compare(mode)
                self.assertEqual({row['class_id']:row['new'] for row in plan['changes']},
                                 {'redes':3.1, 'banco':8.4})
                self.assertEqual({row['class_id']:row['source_file'] for row in plan['changes']},
                                 {'redes':'redes.xlsx', 'banco':'banco.xlsx'})
                self.assertEqual(plan['blocked'], [])

    async def test_project_explicit_subject_in_assessment_sheet_is_also_scoped(self):
        self.set_account([offering('redes'), offering('banco', subject='Banco de Dados')])
        backend.state['imports'] = [source('AT4', subject='Redes de Computadores', class_name='1F',
                                           year=None, period='', source_format='assessment_sheet')]
        plan = await self.compare('AT4')
        self.assertEqual([row['class_id'] for row in plan['changes']], ['redes'])

    async def test_legacy_project_labels_do_not_require_unproven_class_alias(self):
        self.set_account([offering('redes'), offering('banco', subject='Banco de Dados')])
        backend.state['imports'] = [source('AT4', subject='', class_name='1F', year=None, period='', source_format='assessment_sheet')]
        plan = await self.compare('AT4')
        self.assertEqual(len(plan['changes']), 2)

    async def test_exam_without_explicit_class_cannot_choose_between_two_offerings(self):
        for mode in ('A11', 'A12', 'A13', 'A14'):
            with self.subTest(mode=mode):
                self.set_account([offering('redes-a', class_name='EConInf-1PMA'),
                                  offering('redes-b', class_name='EConInf-1PMB')])
                # One currently equal grade must not conceal the second possible
                # placement of the same source assessment.
                self.rosters['redes-a'][0]['grade'] = 8.2
                backend.state['imports'] = [source(mode, class_name='1F', year=None,
                                                   period='', source_format='assessment_sheet')]
                plan = await self.compare(mode)
                self.assertEqual(plan['changes'], [])
                self.assertEqual(plan['unchanged'], [])
                self.assertEqual({row.get('class_id') for row in plan['blocked']},
                                 {'redes-a', 'redes-b'})

    async def test_legacy_shared_projects_can_replicate_across_two_matching_offerings(self):
        for mode in ('AT1', 'AT2', 'AT3', 'AT4'):
            with self.subTest(mode=mode):
                self.set_account([offering('redes-a', class_name='EConInf-1PMA'),
                                  offering('redes-b', class_name='EConInf-1PMB')])
                backend.state['imports'] = [source(mode, subject='', class_name='1F', year=None,
                                                   period='', source_format='assessment_sheet')]
                plan = await self.compare(mode)
                self.assertEqual({row['class_id'] for row in plan['changes']},
                                 {'redes-a', 'redes-b'})
                self.assertEqual(len(plan['changes']), 2)
                self.assertTrue(all(row['column']==mode and row['new']==8.2
                                    for row in plan['changes']))
                self.assertEqual(plan['blocked'], [])

    async def test_exact_registration_does_not_drop_leading_zero_or_guess_by_name(self):
        self.set_account([offering()], ra='050000001')
        backend.state['imports'] = [source(ra='50000001')]
        plan = await self.compare()
        self.assertEqual(plan['changes'], [])
        self.assertTrue(plan['blocked'])

    async def test_cancelled_source_and_target_blank_and_conflict_never_plan_writes(self):
        cases = ([source(status='Cancelado')], [source(value=None)],
                 [source(value=3.2, filename='a.xlsx'), source(value=9.1, filename='b.xlsx')])
        for sources in cases:
            with self.subTest(sources=len(sources)):
                self.set_account([offering()])
                backend.state['imports'] = sources
                plan = await self.compare()
                self.assertEqual(plan['changes'], [])
                self.assertTrue(plan['blocked'])
        self.set_account([offering()], target_status='Cancelado')
        backend.state['imports'] = [source()]
        plan = await self.compare()
        self.assertEqual(plan['changes'], [])
        self.assertTrue(plan['blocked'])

    async def test_source_subject_requires_exact_name_or_unique_real_acronym(self):
        self.set_account([offering('pdm', subject='Programação para Dispositivo Móvel')])
        backend.state['imports'] = [source(subject='PDM')]
        plan = await self.compare()
        self.assertEqual(len(plan['changes']), 1)
        self.assertTrue(backend.matches_discipline('programacao para dispositivo movel', 'Programação para Dispositivo Móvel'))
        self.assertFalse(backend.matches_discipline('Programação', 'Programação para Dispositivo Móvel'))

    async def test_ambiguous_acronym_between_accessible_subjects_is_blocked(self):
        self.set_account([offering('redes', subject='Redes de Computadores'),
                          offering('raciocinio', subject='Raciocínio Computacional')])
        backend.state['imports'] = [source(subject='RC')]
        plan = await self.compare()
        self.assertEqual(plan['changes'], [])
        self.assertTrue(plan['blocked'])


if __name__=='__main__':
    unittest.main()
