"""Observable grade progress without exposing records or changing Save semantics."""
import asyncio
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from notasync.portal import PortalAdapter, PortalError


class PortalProgressTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.adapter = PortalAdapter({'validated': True, 'target_year': 2026})
        self.rows = [
            {'registration': '90000001', 'name': 'Aluno Sintético Um', 'status': 'Matriculado', 'grade': 5.0},
            {'registration': '90000002', 'name': 'Aluna Sintética Dois', 'status': 'Matriculado', 'grade': 6.0},
            {'registration': '90000003', 'name': 'Controle Cancelado', 'status': 'Cancelado', 'grade': None},
        ]
        self.saved = copy.deepcopy(self.rows)
        self.offering = {'id': 'synthetic', 'class_name': 'TESTE-1A', 'discipline': 'Redes', 'year': 2026}
        self.changes = [
            {'registration': '90000001', 'old': 5.0, 'new': 8.3, 'column': 'AT4'},
            {'registration': '90000002', 'old': 6.0, 'new': 0.0, 'column': 'AT4'},
        ]
        self.adapter._open_offering = AsyncMock()
        self.adapter._wait_extract = AsyncMock()
        self.adapter._verify_metadata = AsyncMock()

        async def collect(assessment=None):
            return copy.deepcopy(self.rows)

        async def find(ra, assessment=None):
            return None, copy.deepcopy(next(row for row in self.rows if row['registration'] == ra))

        async def edit(frame, row, new, assessment=None):
            next(item for item in self.rows if item['registration'] == row['registration'])['grade'] = new

        async def save():
            self.saved = copy.deepcopy(self.rows)

        async def reload(**kwargs):
            self.rows = copy.deepcopy(self.saved)

        self.adapter._collect = collect
        self.adapter._find_cell = find
        self.adapter._edit = AsyncMock(side_effect=edit)
        self.adapter._save = AsyncMock(side_effect=save)
        self.adapter.page = SimpleNamespace(reload=AsyncMock(side_effect=reload))

    async def test_default_none_does_not_change_confirmed_results_or_configuration(self):
        self.assertIsNone(self.adapter.progress_callback)
        self.assertNotIn('progress_callback', self.adapter.config)
        results = await self.adapter.apply_offering(self.offering, self.changes)
        self.assertTrue(all(row['status'] == 'confirmed' and row['verified'] for row in results))
        self.adapter._save.assert_awaited_once()
        self.adapter.page.reload.assert_awaited_once()
        self.assertEqual(self.rows[2]['grade'], None)

    async def test_sync_callback_reports_edits_then_save_then_persisted_checks(self):
        events, observed_values = [], []

        def observe(event):
            events.append(event)
            if event['phase'] == 'fill' and event['completed']:
                observed_values.append(self.rows[event['completed'] - 1]['grade'])
            if event['phase'] == 'verify':
                self.assertEqual(self.adapter.page.reload.await_count, 1)

        self.adapter.progress_callback = observe
        results = await self.adapter.apply_offering(self.offering, self.changes)
        self.assertEqual(observed_values, [8.3, 0.0])
        self.assertEqual([(e['phase'], e['completed'], e['total']) for e in events], [
            ('fill', 0, 2), ('fill', 1, 2), ('fill', 2, 2),
            ('save', 0, 1), ('save', 1, 1),
            ('verify', 0, 2), ('verify', 1, 2), ('verify', 2, 2),
        ])
        self.assertTrue(all(set(event) == {'phase', 'completed', 'total', 'assessment'} for event in events))
        self.assertTrue(all(event['assessment'] == 'AT4' for event in events))
        self.assertTrue(all(row['verified'] for row in results))
        text = repr(events)
        for secret in ('90000001', '90000002', 'Aluno Sintético', 'registration', 'name', 'new', 'old'):
            self.assertNotIn(secret, text)

    async def test_async_callback_is_awaited_and_count_bounded(self):
        events = []

        async def observe(event):
            await asyncio.sleep(0)
            events.append(event)

        self.adapter.progress_callback = observe
        await self.adapter.apply_offering(self.offering, self.changes)
        self.assertEqual(len(events), 8)
        self.assertEqual(events[-1]['phase'], 'verify')
        self.assertEqual(events[-1]['completed'], 2)
        self.assertTrue(all(0 <= e['completed'] <= e['total'] for e in events))

    async def test_broken_sync_callback_never_retries_or_changes_success(self):
        def broken(event):
            event['completed'] = 999
            raise RuntimeError('Display is unavailable')

        self.adapter.progress_callback = broken
        results = await self.adapter.apply_offering(self.offering, self.changes)
        self.assertTrue(all(row['status'] == 'confirmed' for row in results))
        self.adapter._save.assert_awaited_once()
        self.assertEqual(self.adapter._edit.await_count, 2)
        self.assertEqual([row['new'] for row in results], [8.3, 0.0])

    async def test_broken_async_callback_never_retries_or_changes_uncertain_save(self):
        async def broken(event):
            await asyncio.sleep(0)
            raise ValueError('Display failure')

        self.adapter.progress_callback = broken
        self.adapter._save = AsyncMock(side_effect=RuntimeError('Save response unavailable'))
        results = await self.adapter.apply_offering(self.offering, self.changes)
        self.assertTrue(all(row['status'] == 'unknown' and not row['verified'] for row in results))
        self.adapter._save.assert_awaited_once()
        self.assertEqual(self.adapter._edit.await_count, 2)
        self.adapter.page.reload.assert_not_awaited()

    async def test_stale_or_cancelled_preflight_never_claims_edit_progress(self):
        events = []
        self.adapter.progress_callback = events.append
        for changes in ([{**self.changes[0], 'old': 1.0}],
                        [{'registration': '90000003', 'old': None, 'new': 9.0}]):
            with self.assertRaises(PortalError):
                await self.adapter.apply_offering(self.offering, changes)
        self.assertEqual(events, [])
        self.adapter._edit.assert_not_awaited()
        self.adapter._save.assert_not_awaited()

    async def test_persistence_mismatch_reports_checks_but_not_verified_results(self):
        self.adapter._save = AsyncMock()
        events = []
        self.adapter.progress_callback = events.append
        results = await self.adapter.apply_offering(self.offering, self.changes)
        self.assertTrue(all(row['status'] == 'unknown' and not row['verified'] for row in results))
        self.assertEqual(events[-1], {'phase': 'verify', 'completed': 2, 'total': 2, 'assessment': 'AT4'})
        self.adapter._save.assert_awaited_once()

    async def test_report_filters_private_payload_and_invalid_event_counts(self):
        events = []
        self.adapter.progress_callback = events.append
        await self.adapter._report_progress({'phase': 'fill', 'completed': 8, 'total': 3, 'assessment': 'AT1',
                                             'name': 'Private person', 'registration': '90000001'})
        await self.adapter._report_progress({'phase': 'verify', 'completed': -4, 'total': 3, 'assessment': 'AT1'})
        self.assertEqual(events, [
            {'phase': 'fill', 'completed': 3, 'total': 3, 'assessment': 'AT1'},
            {'phase': 'verify', 'completed': 0, 'total': 3, 'assessment': 'AT1'},
        ])
        for malformed in ({'phase': 'fill', 'completed': True, 'total': 1, 'assessment': 'AT1'},
                          {'phase': 'fill', 'completed': 0, 'total': -1, 'assessment': 'AT1'},
                          {'phase': 'fill', 'completed': 0, 'total': 1, 'assessment': 'MB1'},
                          {'phase': 'private', 'completed': 0, 'total': 1, 'assessment': 'AT1'}):
            await self.adapter._report_progress(malformed)
        self.assertEqual(len(events), 2)


if __name__ == '__main__':
    unittest.main()
