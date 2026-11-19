from multiprocessing import freeze_support

if __name__ == '__main__':
    # Must precede GUI imports: frozen multiprocessing children must never
    # interpret their private bootstrap arguments as a new desktop window.
    freeze_support()
    import sys
    if '--update-helper' in sys.argv[1:]:
        import argparse
        parser=argparse.ArgumentParser(add_help=False)
        parser.add_argument('--update-helper',required=True)
        parser.add_argument('--update-token',required=True)
        args=parser.parse_args()
        from smart_tapo.update_install import apply_update
        raise SystemExit(apply_update(args.update_helper,args.update_token))
    from smart_tapo.desktop import main
    raise SystemExit(main())
