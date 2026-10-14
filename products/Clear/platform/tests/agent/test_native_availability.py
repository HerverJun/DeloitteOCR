"""Keep declaration, verified service identity and callable tools distinct."""
from threading import RLock
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock

from packages.contracts.models import AppManifest
from workbench_platform.agent.registry import ToolRegistry
from workbench_platform.agent.tools.native_catalog import register
from workbench_platform.application_services.native import availability, prepare
from workbench_platform.catalog.launch import LaunchStatus
from workbench_platform.catalog.registry import AppRegistry, CatalogSnapshot, Installation
from workbench_platform.supervisor.processes import Supervisor


def services_with(states):
    installations = {
        family: SimpleNamespace(installed=value != 'not_installed')
        for family, value in states.items()
    }

    def current(family):
        state = states[family]
        if state == 'ready':
            return SimpleNamespace(instance_id=family)
        raise LaunchStatus(int(state), 'APP_UNAVAILABLE')

    return SimpleNamespace(job_service=object(),
        supervisor=SimpleNamespace(registry=SimpleNamespace(installations=installations)),
        launch_service=SimpleNamespace(current=Mock(side_effect=current), start=Mock()))


def health_registry(resources, *, installed):
    manifest = AppManifest(schema_version='1.0', app_id='ocr', display_name='OCR',
        app_version='test', adapter_version='0.1.0', min_platform_version='0.1.0',
        data_schema_version='test', entry_mode='standalone_page', capabilities=[],
        input_types=[], output_types=[], resources=resources, license_refs=[])
    registry = AppRegistry.__new__(AppRegistry)
    registry._lock = RLock()
    registry._observations = {}
    registry._snapshot = CatalogSnapshot({'ocr': manifest},
        {'ocr': Installation('ocr', installed, 'Bundle is not installed')}, {}, 1)
    return registry


class NativeAvailabilityTests(TestCase):
    def test_missing_services_and_uninstalled_apps_never_claim_ready(self):
        self.assertEqual(availability(SimpleNamespace(), 'ocr')['state'], 'unavailable')
        services = services_with({'ocr': 'not_installed', 'classification': 'not_installed'})
        registry = ToolRegistry()
        register(registry)
        for name in registry.tools:
            self.assertEqual(registry.describe(name, services)['availability']['state'], 'unavailable')
        services.launch_service.current.assert_not_called()
        services.launch_service.start.assert_not_called()

    def test_not_running_404_is_preparable_without_starting_it(self):
        services = services_with({'ocr': '404', 'classification': '404'})
        self.assertEqual(availability(services, 'ocr')['state'], 'preparable')
        services.launch_service.start.assert_not_called()

    def test_unverified_503_is_unavailable_and_prepare_does_not_start(self):
        services = services_with({'ocr': '503', 'classification': 'ready'})
        self.assertEqual(availability(services, 'ocr')['state'], 'unavailable')
        ctx = SimpleNamespace(services=services, current=Mock())
        with self.assertRaises(ValueError):
            prepare(ctx, 'ocr')
        services.launch_service.start.assert_not_called()

    def test_other_launch_faults_are_not_mapped_to_a_startable_service(self):
        services = services_with({'ocr': '410', 'classification': 'ready'})
        self.assertEqual(availability(services, 'ocr')['state'], 'unavailable')
        services.launch_service.start.assert_not_called()

    def test_generic_tools_preserve_each_application_status(self):
        services = services_with({'ocr': 'ready', 'classification': 'not_installed'})
        registry = ToolRegistry()
        register(registry)
        for name in registry.tools:
            if name == 'classification_import_assets':
                continue
            state = registry.describe(name, services)['availability']
            self.assertEqual(state['state'], 'conditional')
            self.assertEqual(state['applications']['ocr']['state'], 'ready')
            self.assertEqual(state['applications']['classification']['state'], 'unavailable')
        services.launch_service.start.assert_not_called()

    def test_mixed_running_and_startable_services_require_prepare(self):
        services = services_with({'ocr': 'ready', 'classification': '404'})
        registry = ToolRegistry()
        register(registry)
        state = registry.describe('native_list_projects', services)['availability']
        self.assertEqual(state['state'], 'preparable')
        self.assertEqual(state['applications']['ocr']['state'], 'ready')
        self.assertEqual(state['applications']['classification']['state'], 'preparable')
        self.assertIn('classification_prepare', state['reason'])
        services.launch_service.start.assert_not_called()

    def test_classification_import_does_not_inherit_ocr_readiness(self):
        services = services_with({'ocr': 'ready', 'classification': '503'})
        registry = ToolRegistry()
        register(registry)
        self.assertEqual(registry.describe('classification_import_assets', services)
            ['availability']['state'], 'unavailable')
        services.launch_service.start.assert_not_called()

    def test_uninstalled_gpu_service_does_not_claim_hardware_is_unavailable(self):
        state = health_registry(['cpu', 'gpu'], installed=False).health('ocr').gpu
        self.assertEqual(state.state, 'unavailable')
        self.assertEqual(state.reason, 'GPU service is not installed')

    def test_service_identity_never_claims_gpu_has_been_probed(self):
        for resources, expected in ((['cpu'], 'disabled'), (['cpu', 'gpu'], 'unknown')):
            with self.subTest(resources=resources):
                registry = health_registry(resources, installed=True)
                supervisor = Supervisor.__new__(Supervisor)
                supervisor._lock = RLock()
                supervisor.registry = registry
                supervisor._current = {}
                supervisor._observe('ocr', 1, 'instance')
                self.assertEqual(registry.health('ocr').service.state, 'ready')
                state = registry.health('ocr').gpu
                self.assertEqual(state.state, expected)
                self.assertEqual(state.reason, 'GPU is not declared' if expected == 'disabled'
                    else 'GPU has not been probed')
                supervisor._unavailable(SimpleNamespace(app_id='ocr', instance_id='instance'), 1)
                self.assertEqual(registry.health('ocr').service.state, 'unavailable')
                self.assertEqual(registry.health('ocr').gpu, state)
