"""
Reference solution (do NOT show to the candidate; a temporary file for task QA).
Implements every requirement from task.md: early stopping that restores the best
weights, a checkpoint after each epoch, resume that restores optimizer and random
state, and safe checkpoint loading.
"""
import math
import os
import random
import torch
import torch.nn as nn

from model import MLP
from data import make_data


def _save_checkpoint(path, model, optimizer, epoch, best_metric, best_state, rng_state):
    torch.save(
        {
            "model": model.state_dict(),
            "optimizer": optimizer.state_dict(),
            "epoch": epoch,
            "best_metric": best_metric,
            "best_state": best_state,
            "torch_rng": rng_state["torch"],
            "python_rng": rng_state["python"],
        },
        path,
    )


def _eval(model, loader, loss_fn, device):
    model.eval()
    total, count = 0.0, 0
    with torch.no_grad():
        for x, y in loader:
            x, y = x.to(device), y.to(device)
            pred = model(x)
            total += loss_fn(pred, y).item() * x.size(0)
            count += x.size(0)
    return total / count


def train(
    model: nn.Module,
    train_loader,
    val_loader,
    n_epochs: int = 20,
    lr: float = 1e-3,
    patience: int = 3,
    checkpoint_path: str = "checkpoint.pt",
) -> nn.Module:
    device = "cpu"
    model = model.to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=lr)
    loss_fn = nn.MSELoss()

    start_epoch = 0
    best_metric = math.inf
    best_state = None
    epochs_no_improve = 0

    if os.path.exists(checkpoint_path):
        # weights_only=False: the checkpoint holds non-tensor objects (RNG state,
        # best_metric) — on torch>=2.6 the default weights_only=True would raise
        # UnpicklingError loading them (this is task-01 trap 3).
        ckpt = torch.load(checkpoint_path, map_location=device, weights_only=False)
        model.load_state_dict(ckpt["model"])
        optimizer.load_state_dict(ckpt["optimizer"])
        start_epoch = ckpt["epoch"] + 1
        best_metric = ckpt["best_metric"]
        best_state = ckpt["best_state"]
        torch.set_rng_state(ckpt["torch_rng"])
        random.setstate(ckpt["python_rng"])
        print(f"[resume] from epoch {start_epoch}, best_metric={best_metric:.4f}")

    for epoch in range(start_epoch, n_epochs):
        model.train()
        train_loss, count = 0.0, 0
        for x, y in train_loader:
            x, y = x.to(device), y.to(device)
            optimizer.zero_grad()
            pred = model(x)
            loss = loss_fn(pred, y)
            loss.backward()
            optimizer.step()
            train_loss += loss.item() * x.size(0)
            count += x.size(0)
        train_loss /= count

        val_loss = _eval(model, val_loader, loss_fn, device)
        improved = val_loss < best_metric

        if improved:
            best_metric = val_loss
            best_state = {k: v.detach().clone() for k, v in model.state_dict().items()}
            epochs_no_improve = 0
        else:
            epochs_no_improve += 1

        rng_state = {"torch": torch.get_rng_state(), "python": random.getstate()}
        _save_checkpoint(checkpoint_path, model, optimizer, epoch, best_metric, best_state, rng_state)

        print(
            f"epoch {epoch:02d} | train {train_loss:.4f} | val {val_loss:.4f}"
            f" | best {best_metric:.4f}{' *' if improved else ''}"
        )

        if epochs_no_improve >= patience:
            print(f"[early stop] no improvement {patience} epochs")
            break

    if best_state is not None:
        model.load_state_dict(best_state)
    return model


if __name__ == "__main__":
    import time
    t0 = time.time()
    model = MLP()
    train_loader, val_loader = make_data()
    trained = train(model, train_loader, val_loader)
    print(f"elapsed: {time.time() - t0:.1f}s")
