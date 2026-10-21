"""Concurrent cold restoration must reuse the verified native launch."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from workbench_platform.application_services.native import prepare
from workbench_platform.catalog.launch import LaunchStatus
from workbench_platform.agent.store import RunSuperseded


class NativePrepareRaceTests(unittest.TestCase):
    def setup_context(self, race):
        live = SimpleNamespace(instance_id='actual-live-instance')
        launch = Mock()
        launch.current.side_effect = [LaunchStatus(404, 'NOT_FOUND'), live]
        launch.start.side_effect = race
        workspaces = Mock()
        workspaces.list_links.return_value = []
        workspaces.get_workspace.return_value = SimpleNamespace(name='Owned project')
        services = SimpleNamespace(launch_service=launch, workspace_store=workspaces, job_service=Mock())
        context = SimpleNamespace(current=Mock(), services=services, workspace_id='wOwned', operation_id='originalOp')
        projects = Mock()
        projects.create.return_value = {'link_id':'boundLink','project_id':'nativeProject'}
        services.job_service._adapter.return_value._request.return_value = {'engines': {'ppocr': {'ready':True}}}
        services.job_service._adapter.return_value.capabilities.return_value = SimpleNamespace()
        return context, projects, live

    def test_raced_restore_reuses_authoritative_current_and_creates_project_once(self):
        context, projects, live = self.setup_context(LaunchStatus(409, 'RESOURCE_BUSY'))
        with patch('workbench_platform.application_services.native.availability',return_value={'state':'preparable'}), \
             patch('workbench_platform.application_services.native.OcrProjects',return_value=projects), \
             patch('workbench_platform.application_services.native.asdict',return_value={}):
            value = prepare(context,'ocr')
        self.assertEqual(value['instance_id'],live.instance_id)
        context.services.launch_service.start.assert_called_once()
        self.assertEqual(context.services.launch_service.current.call_count,2)
        projects.create.assert_called_once_with('wOwned','Owned project',request_id='originalOp')

    def test_unverified_current_and_other_launch_faults_are_not_retried(self):
        for status, code in ((409,'IDEMPOTENCY_CONFLICT'),(503,'APP_UNAVAILABLE'),(410,'APP_UNAVAILABLE')):
            with self.subTest(code=code,status=status):
                context, projects, _ = self.setup_context(LaunchStatus(status,code))
                with patch('workbench_platform.application_services.native.availability',return_value={'state':'preparable'}), \
                     patch('workbench_platform.application_services.native.OcrProjects',return_value=projects):
                    with self.assertRaises(LaunchStatus):prepare(context,'ocr')
                projects.create.assert_not_called()
                self.assertEqual(context.services.launch_service.current.call_count,1)
        context, projects, _ = self.setup_context(LaunchStatus(409,'RESOURCE_BUSY'))
        context.services.launch_service.current.side_effect = [LaunchStatus(404,'NOT_FOUND'),LaunchStatus(503,'APP_UNAVAILABLE')]
        with patch('workbench_platform.application_services.native.availability',return_value={'state':'preparable'}), \
             patch('workbench_platform.application_services.native.OcrProjects',return_value=projects):
            with self.assertRaises(LaunchStatus):prepare(context,'ocr')
        projects.create.assert_not_called()

    def test_stop_during_launch_prevents_downstream_native_project_writes(self):
        context, projects, _ = self.setup_context(LaunchStatus(409,'RESOURCE_BUSY'))
        context.current.side_effect = [None,RunSuperseded('stopped')]
        with patch('workbench_platform.application_services.native.availability',return_value={'state':'preparable'}), \
             patch('workbench_platform.application_services.native.OcrProjects',return_value=projects):
            with self.assertRaises(RunSuperseded):prepare(context,'ocr')
        projects.create.assert_not_called()
        self.assertEqual(context.services.launch_service.current.call_count,1)


if __name__ == '__main__':
    unittest.main()
