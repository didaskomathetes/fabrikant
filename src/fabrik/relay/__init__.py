"""Relay-Tasks (ADR 0037): langlaufende Aufgaben als Kette kurzer Agent-Sessions.

Ein GitHub-Issue (Label `fabrik-relay`) ist der Auftrag, eine Handoff-Datei auf dem Branch
`fabrik/relay/<N>` das Gedächtnis zwischen den Sessions. Projekte schalten das Feature mit einem
`relay:`-Block in der Spec frei (`config.expand_loops`); Logik in `model`, I/O in `github`,
`workspace` und `commands`.
"""
