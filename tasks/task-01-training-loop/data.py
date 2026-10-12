"""Synthetic regression dataset."""
import torch
from torch.utils.data import TensorDataset, DataLoader


def make_data(
    n_train: int = 500,
    n_val: int = 100,
    n_features: int = 10,
    batch_size: int = 32,
    seed: int = 42,
) -> tuple[DataLoader, DataLoader]:
    g = torch.Generator().manual_seed(seed)

    X_train = torch.randn(n_train, n_features, generator=g)
    X_val = torch.randn(n_val, n_features, generator=g)
    w = torch.randn(n_features, 1, generator=g)

    y_train = X_train @ w + 0.1 * torch.randn(n_train, 1, generator=g)
    y_val = X_val @ w + 0.1 * torch.randn(n_val, 1, generator=g)

    train_ds = TensorDataset(X_train, y_train)
    val_ds = TensorDataset(X_val, y_val)

    return (
        DataLoader(train_ds, batch_size=batch_size, shuffle=True),
        DataLoader(val_ds, batch_size=batch_size, shuffle=False),
    )
