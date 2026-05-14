"""Phase-2 aggregators.

Aggregators consume ``Signal`` streams from extractors (per
``faultline.protocols.Aggregator``) and produce one component of an
``AggregateResult``. While the engine still runs through the legacy
``faultline.llm.pipeline.run`` path, individual aggregators can be
called directly with the DeepScanResult shape — see ``critique`` for
the first such consumer.
"""
