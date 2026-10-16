def pytest_configure(config):
    config.addinivalue_line("markers", "req(*ids): requirement IDs a test covers")
