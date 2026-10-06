"""Mock providers (APP_SPEC 7.8 and 16): deterministic, offline, same signatures as the real adapters.

Each mock derives every output from ``sha256(canonical request)``, records every request in ``.requests``, adds
``CostEntry``-like rows with ``provider="mock"`` and honours ``DUOSKIN_MOCK_FAULTS`` (see ``duoskin.providers.faults``).
Nothing here touches the network. Assets made from mock output must be tagged ``source="mock"`` by the caller.
"""
