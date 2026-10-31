"""Common command entrypoint; method-specific beginner guides link here."""
import argparse
import json
import math
from pathlib import Path
from .policy_catalog import MODELS, catalog, require_method


def main():
    parser=argparse.ArgumentParser(description='LeRobot export and RTC / Training-time RTC / VLASH / A2C2 training')
    commands=parser.add_subparsers(dest='command',required=True)
    commands.add_parser('models', help='Show model/method capabilities without importing ML dependencies')
    p=commands.add_parser('export');p.add_argument('--dataset',required=True);p.add_argument('--output',required=True)
    p.add_argument('--encoder',choices=['h264_nvenc','libx264'],default='h264_nvenc')
    p=commands.add_parser('train')
    p.add_argument('--method',choices=['standard','rtc','training_time_rtc','vlash'],required=True)
    for key in ('dataset','output','model-id'):p.add_argument('--'+key,required=True)
    p.add_argument('--config', help='Defaults to the runtime profile for --method')
    p.add_argument('--policy', choices=list(MODELS), default='flow', help='Model family (default: existing compact flow)')
    p.add_argument('--pretrained', help='Override the family default with a Hugging Face ID or local checkpoint')
    p.add_argument('--policy-config', help='JSON file with advanced LeRobot model config overrides')
    p.add_argument('--max-delay-steps', type=int, help='Override the training and runtime delay budget')
    p.add_argument('--max-inference-s', type=float, help='Override the runtime inference deadline')
    p.add_argument('--validation-fraction', type=float, help='Use 0 to train on all source episodes')
    p.add_argument('--save-every', type=int, default=1000, help='LeRobot checkpoint interval in updates')
    p.add_argument('--keep-checkpoints', type=int, default=2, help='Number of periodic LeRobot checkpoints to retain')
    p.add_argument('--image-cache-gb', type=float, default=0, help='Decoded image cache in GiB; 0 preserves the small default')
    p.add_argument('--steps',type=int,default=20000);p.add_argument('--batch-size',type=int,default=8)
    p.add_argument('--device',default='cuda');p.add_argument('--seed',type=int,default=42)
    p.add_argument('--dim-model',type=int,default=128);p.add_argument('--n-heads',type=int,default=4)
    p.add_argument('--n-layers',type=int,default=2);p.add_argument('--denoise-steps',type=int,default=10)
    p=commands.add_parser('prepare-a2c2')
    for key in ('model','dataset','output'):p.add_argument('--'+key,required=True)
    p.add_argument('--device',default='cuda');p.add_argument('--stride',type=int,default=4)
    p.add_argument('--max-samples',type=int,default=1000000);p.add_argument('--seed',type=int,default=42)
    p.add_argument('--max-delay-steps',type=int)
    p=commands.add_parser('train-a2c2')
    for key in ('cache','output'):p.add_argument('--'+key,required=True)
    p.add_argument('--device',default='cuda');p.add_argument('--steps',type=int,default=5000)
    p.add_argument('--batch-size',type=int,default=256);p.add_argument('--hidden',type=int,default=128)
    p.add_argument('--seed',type=int,default=42)
    p=commands.add_parser('evaluate')
    for key in ('model','dataset','output'):p.add_argument('--'+key,required=True)
    p.add_argument('--device',default='cuda');p.add_argument('--samples',type=int,default=64)
    p.add_argument('--delay-steps',type=int,default=3);p.add_argument('--correction')
    p.add_argument('--smoothing', choices=['none', 'three_point'], default='none',
                   help='Evaluate with the same inference smoothing as runtime (default: none)')
    args=parser.parse_args()
    if args.command == 'models':
        print(json.dumps(catalog(), indent=2)); return
    if args.command == 'train':
        try:
            require_method(args.policy, args.method)
        except ValueError as exc:
            parser.error(str(exc))
        if args.policy == 'flow' and (args.pretrained or args.policy_config):
            parser.error('--pretrained/--policy-config require a LeRobot --policy')
        args.auto_config = args.config is None
        if args.auto_config:
            args.config = str(Path(__file__).resolve().parents[4]/'config/runtime'/
                              f'system.real30hz.640x480.{args.method}.yaml')
        if args.max_delay_steps is not None and args.max_delay_steps < 0:
            parser.error('max-delay-steps must be nonnegative')
        if args.max_inference_s is not None and (not math.isfinite(args.max_inference_s) or args.max_inference_s <= 0):
            parser.error('max-inference-s must be positive')
        if args.validation_fraction is not None and not 0 <= args.validation_fraction < 1:
            parser.error('validation-fraction must be in [0,1)')
        if not math.isfinite(args.image_cache_gb) or args.image_cache_gb < 0:
            parser.error('image-cache-gb must be finite and nonnegative')
    for key in ('steps','batch_size','stride','max_samples','hidden','n_layers','n_heads','dim_model','denoise_steps',
                'save_every','keep_checkpoints'):
        if hasattr(args,key) and getattr(args,key)<1:parser.error(key+' must be positive')
    if args.command=='evaluate' and (args.samples<1 or args.delay_steps<0):parser.error('samples must be positive and delay-steps nonnegative')
    if args.command=='export':
        from .lerobot_io import export_lerobot
        print(export_lerobot(args.dataset,args.output,args.encoder))
    else:
        if args.command == 'train' and args.policy != 'flow':
            from .lerobot_policy import train_lerobot
            train_lerobot(args); return
        from .method_training import train_flow,prepare_correction,train_correction,evaluate_model
        {'train':train_flow,'prepare-a2c2':prepare_correction,'train-a2c2':train_correction,'evaluate':evaluate_model}[args.command](args)


if __name__=='__main__':main()
