# Seat Reservation Service

## Context

A booking service holds a limited pool of seats (or stock units). Many clients hit it
at the same time trying to reserve from the same pool. They call over an unreliable
network and retry requests that time out, so the same reservation can arrive more than
once. Your job is the core reservation logic: decide whether a request can be satisfied
and update the available stock. The catch is that this runs concurrently — the obvious
"read the count, check it, write the new count" is not as safe as it looks.

## What's given

- `store.py` — the backing `Store` (stands in for a database). It exposes async
  `get(item_id)` / `set(item_id, units)` accessors. Don't touch it.
- `app.py` — the `reserve(store, item_id, qty, request_id)` stub. Write your
  implementation here. There's no HTTP server on purpose: you implement and verify the
  core logic directly, the way the request handler would call it.
- `pyproject.toml` — project metadata.

## What to implement

Implement `reserve(...)` in `app.py` so that:

1. **Reserve when available.** If `item_id` has at least `qty` units, reserve them
   (decrement the stock) and return `ok=True` with the remaining count.
2. **Reject when not.** If there isn't enough stock, reserve nothing and return
   `ok=False`. A reservation is all-or-nothing.
3. **Never oversell.** Across any number of concurrent reservations, the service must
   never confirm more units than existed; available stock must never go negative.

## How to run

`app.py` has no entry point of its own — you exercise `reserve(...)` by constructing a
`Store` with some stock and calling it, the way the request handler would. Write a small
script (or use a REPL) to try it, including firing several reservations at the same pool
at once.

## What counts as done

- Single client: reserving within stock returns `ok=True` and decrements the pool;
  reserving more than is available returns `ok=False` and leaves the pool unchanged.
- Concurrency: fire many reservations of one unit at the same small pool at once — the
  number confirmed never exceeds the starting stock. If more is confirmed than existed,
  concurrent requests slipped through and the pool was oversold.

## Time

45–60 minutes.

You have an AI assistant.
