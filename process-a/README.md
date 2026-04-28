# Process A — sampler

This directory is **reserved for Process A**, the sampler daemon that reads ADCs / load cells on the Raspberry Pi and emits weight readings as UDP datagrams to Process B.

Process A is owned by a separate teammate. Code will land here once that work begins.

## Contract with Process B

Process A sends one reading per UDP datagram, JSON UTF-8, to Process B's listener port:

```json
{
  "shelf_id": "550e8400-e29b-41d4-a716-446655440000",
  "scale_index": 0,
  "est_grams": 750.2,
  "sampled_at": "2026-04-21T08:30:00Z"
}
```

Process A also listens on its own control port for `wake` commands from Process B:

```json
{ "type": "wake" }
```

Sleep is handled locally by Process A via an inactivity timer; Process B never sends sleep commands.

## See also

- [`../process-b/`](../process-b/) — the broker/persister daemon that consumes these datagrams.
- [`../README.md`](../README.md) — system overview.
