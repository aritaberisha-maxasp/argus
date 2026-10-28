"""Which DATEV the contract tests run against.

By default an in-process fake DATEV. Set ``ARGUS_DATEV_CONTRACT_URL`` to run the same
tests against a fake in Docker, or later against the bridge to Max ASP's DATEV test
client. That DATEV must hold the fixture vendors and customers from
tests/fixtures/master-data and the postings from tests/fixtures/datev/postings.csv.
"""

import os
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from argus.datev import DatevConnectGateway
from argus.datev.fake import DEFAULT_CLIENT_ID, create_fake_datev


@pytest.fixture
def gateway() -> Iterator[DatevConnectGateway]:
    url = os.environ.get("ARGUS_DATEV_CONTRACT_URL")
    client_id = os.environ.get("ARGUS_DATEV_CONTRACT_CLIENT", DEFAULT_CLIENT_ID)
    if url:
        remote = DatevConnectGateway.from_url(url, client_id)
        yield remote
        return
    with TestClient(create_fake_datev()) as http:
        yield DatevConnectGateway(http, DEFAULT_CLIENT_ID)
