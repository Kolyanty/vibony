"""
Starter code for the training loop.
Implement the `train` function per the requirements in task.md.
"""
import torch
from model import MLP
from data import make_data


def train(
    model: torch.nn.Module,
    train_loader,
    val_loader,
    n_epochs: int = 20,
    lr: float = 1e-3,
    patience: int = 3,
    checkpoint_path: str = "checkpoint.pt",
) -> torch.nn.Module:
    """
    Train the model. See task.md for the requirements.

    Returns the trained model.
    """
    raise NotImplementedError("Implement me")


if __name__ == "__main__":
    model = MLP()
    train_loader, val_loader = make_data()
    trained = train(model, train_loader, val_loader)
