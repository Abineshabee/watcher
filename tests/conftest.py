import sys

# Force watcher modules to reload so their module-level lines
# execute under the coverage tracer, not before it starts.
for mod_name in list(sys.modules):
    if mod_name.startswith("watcher"):
        del sys.modules[mod_name]
