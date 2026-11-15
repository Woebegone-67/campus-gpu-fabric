"""Proof-only equivalent of adding the missed key to the existing test's restore list."""
def pytest_runtest_call(item):
    if item.name == 'test_historical_report_configuration_survives_environment_cleanup':
        item.funcargs['monkeypatch'].setenv('FABRIC_ACCOUNTS_FILE', 'untrusted-inherited-value')
