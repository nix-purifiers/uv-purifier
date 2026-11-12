def build_wheel(*args, **kwargs):
    raise RuntimeError('inactive Windows editable source wheel was built')


def build_editable(*args, **kwargs):
    raise RuntimeError('inactive Windows editable source was installed')
