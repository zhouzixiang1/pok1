# Neural National Lab

Neural-enhanced national bot experiments.

- `versions/`: complete runnable bot snapshots. Add a new directory for each
  experiment version.
- `tools/`: teacher-data collection, tiny-MLP training, and mirror evaluation.
- `data/`: small generated datasets, metrics, and evaluation reports.
- `external/`: ignored shallow clones for research scans.

Runtime bots stay stdlib-only. Training may use PyTorch/NumPy. The neural model
acts after the rule strategy and before `sanitize_action`; native TCP output is
still produced by the national entrypoint.
