# Training Loop with Checkpointing and Resume

## Context

There is a ready model (MLP regression) and a synthetic dataset. You need to write a training loop that can resume after an interruption (crash / Ctrl+C / pod termination) — without losing progress and without artifacts in the final model.

## What's given

- `model.py` — `MLP`. Don't touch.
- `data.py` — `make_data()` returns `(train_loader, val_loader)`. Don't touch.
- `train.py` — stub with the signature `train(model, train_loader, val_loader, n_epochs=20, lr=1e-3, patience=3, checkpoint_path="checkpoint.pt")`. Write your implementation here.
- `pyproject.toml` — dependencies.

## What to implement

Implement `train(...)` in `train.py` so that all requirements are met:

1. **Optimizer and loss:** Adam, `MSELoss`.
2. **Training loop:** each epoch — pass through `train_loader`, then validation on `val_loader`.
3. **Logging:** print epoch number, train loss, val loss on each epoch.
4. **Early stopping:** if val loss doesn't improve for `patience` consecutive epochs — stop training before `n_epochs`.
5. **Checkpoint after each epoch:** save to `checkpoint_path` a state sufficient for resuming.
6. **Resume:** if `checkpoint_path` exists on startup — continue training from there; progress of an interrupted run is not lost.

## How to run

```
poetry install --no-root
poetry run python train.py
```

## What counts as done

- `poetry run python train.py` completes without errors and returns a trained model.
- Resume: delete `checkpoint.pt`, run with `n_epochs=10`, then run again with `n_epochs=20` — should continue from epoch 10, not start from scratch.
- Early stopping: on the default data and parameters val loss falls almost monotonically, so stop by `patience=3` in 20 epochs may not trigger — that's fine. To verify early stopping logic, run with `patience=1` and a sufficiently large `n_epochs` (e.g. 50), verify the loop stops early.

## Time

45–60 minutes.

You have an AI assistant.
