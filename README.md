# Run UC-NeRF on XLD dataset

## File Tree
```bash
mvs
├── ...
nerf
├── ...
pose_refinement
├── ...
exp
├── checkpoints
├────────├────────
data
├── carlc_pic_0603_Town0N
├── ...

```

## 🚀 Start up
Follows the provided `launch.json` to run the trianing script.
```json
{
    "env": {"CUDA_VISIBLE_DEVICES": "7",},
    "name": "Train",
    "type": "python",
    "request": "launch",
    "program": "${workspaceFolder}/nerf/train.py",
    "console": "integratedTerminal",
    "justMyCode": false,
    "args": ["--gin_configs","configs/waymo.gin" ,
}
```