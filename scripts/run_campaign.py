"""Campaign entry point. The only supported way to start a measurement run.

Everything here is a gate. By the time run() is reached, the parameters are
complete, the server is answering, the item files match their recorded hashes,
and the hardware objects have been shown to be something other than the stubs
this repository ships. A ten-day campaign that fails on day six because a price
was None, or that writes twenty thousand rows of zero joules because a stub was
constructed by accident, costs more than every check below put together.

Provenance is printed before the first call, not after the last, so a run whose
identity is later disputed can be settled from terminal scrollback rather than
from a metadata file inside the run being disputed.

Standard library only, Python 3.10 compatible.
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from masenergy import chat, client, config, jetson
from masenergy.records import new_run_id
from masenergy.runner import Runner

STUB_METHODS = {
    client.Trigger: ("high", "low", "status"),
    client.Device: ("wait_for_gate", "read_state",
                    "start_safety_watchdog", "safety_tripped"),
    client.EnergyMeter: ("start", "stop", "measure_idle"),
}


def is_stub(instance, base):
    """True while any measured method is still the one inherited from base.

    Class identity cannot answer this question. NullTrigger, NullDevice and
    NullEnergyMeter are empty subclasses, so a check on type would pass the
    moment someone wrote 'class JetsonTrigger(Trigger): pass' and the run would
    record zero joules against every call with nothing to show it had.

    Any rather than all, because a partly implemented interface is the harder
    accident and the likelier one. A Trigger that drives a line but inherits
    status() emits real pulses and reports pulse zero on every row, which
    destroys the join to the external rig while every energy column still
    looks populated. Demanding that all of them be overridden costs nothing
    and refuses that case at startup.

    close() is not on these lists. It is lifecycle rather than measurement,
    and a real implementation with nothing to release is entitled to inherit
    the no-op.
    """
    return any(
        getattr(type(instance), name, None) is getattr(base, name, None)
        for name in STUB_METHODS[base]
    )


def hardware(dry):
    """The Trigger, Device and EnergyMeter a run will be measured through.

    Under --dry the Null implementations are returned deliberately and the run
    is written somewhere a measurement can never be pooled with it. Otherwise
    the real implementations are returned and checked, and a run refuses to
    start on anything that is still a stub.
    """
    if dry:
        return (client.NullTrigger(), client.NullDevice(),
                client.NullEnergyMeter())

    built = []
    for build in (jetson.JetsonTrigger, jetson.JetsonDevice,
                  jetson.JetsonEnergyMeter):
        try:
            built.append(build())
        except (jetson.ThermalUnavailable, jetson.ina3221.RailsUnavailable,
                jetson.gpio.GpioError) as exc:
            for made in built:
                made.close()
            raise SystemExit(
                "%s\n\nA measurement run has to happen on the Jetson with the "
                "rig attached.\nUse --dry for a structural rehearsal on this "
                "machine." % exc)
    return tuple(built)


def check_hardware(trigger, device, meter):
    """Names of the interfaces that are still stubs, in report order."""
    stubs = []
    for instance, base, label in (
        (trigger, client.Trigger, "Trigger"),
        (device, client.Device, "Device"),
        (meter, client.EnergyMeter, "EnergyMeter"),
    ):
        if is_stub(instance, base):
            stubs.append(label)
    return stubs


def resolve_run(resume, dry):
    """Return the run_id and output directory for this invocation.

    A fresh run_id is timestamped, so an interrupted campaign restarted without
    --resume would write to a new directory, find no completed tasks, and
    silently redo work that is already on disk. Resuming is therefore explicit
    and its run_id is checked: the trailing field of a run_id is the config
    hash it was started under, and rows written under two different hashes are
    not comparable and must not land in one directory.
    """
    if resume:
        expected = config.config_hash()
        if not resume.endswith(expected):
            raise SystemExit(
                "Refusing to resume %s under config %s.\n"
                "That run was started under a different configuration, and rows\n"
                "written under two hashes cannot be pooled. Start a new run, or\n"
                "restore the configuration the run began with."
                % (resume, expected)
            )
        run_id = resume
    else:
        run_id = new_run_id(config.config_hash())
    folder = ("rehearsal-%s" % run_id) if dry else run_id
    return run_id, ROOT / "data" / "raw" / folder


def banner(runner, dry, host, port):
    """Print everything needed to identify this run before it produces a row."""
    print("=" * 72)
    print("MAS ENERGY CAMPAIGN%s" % ("   REHEARSAL, NOT A MEASUREMENT" if dry else ""))
    print("=" * 72)
    print("  run_id        %s" % runner.run_id)
    print("  config_hash   %s" % config.config_hash())
    print("  prompts_hash  %s" % chat.prompts_hash())
    for name in config.DATASETS:
        payload = runner.items[name]
        print("  items %-9s %s  (%d items)"
              % (name, payload["sha256"], len(payload["items"])))
    print("  server        http://%s:%d" % (host, port))
    print("  output        %s" % runner.out)
    print("  blocks        %d" % len(config.blocks()))
    print("  tasks         %d" % (len(config.blocks()) * config.N_ITEMS
                                  * len(config.SEEDS)))
    print("  est. calls    %d" % config.estimated_calls())
    if dry:
        print()
        print("  Null hardware. Every energy and temperature column will be zero.")
        print("  Written to a rehearsal directory so it can never be pooled with")
        print("  a measurement run.")
    print("=" * 72)
    print()


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Start or resume the measurement campaign.")
    ap.add_argument("--dry", action="store_true",
                    help="structural rehearsal with the Null hardware "
                         "implementations, written to a rehearsal directory")
    ap.add_argument("--resume", default="",
                    help="run_id of an interrupted campaign to continue")
    ap.add_argument("--port", type=int, default=config.SERVER_PORT)
    ap.add_argument("--host", default=config.SERVER_HOST)
    a = ap.parse_args(argv)

    try:
        config.validate()
    except RuntimeError as exc:
        raise SystemExit(
            "%s\n\nEvery parameter above must be measured or chosen before a "
            "run starts.\nThey are checked here and again in Runner.run(); "
            "--dry does not exempt\na run from them, because a rehearsal under "
            "a configuration the campaign\ncannot use is not rehearsing the "
            "campaign." % exc
        )

    trigger, device, meter = hardware(a.dry)
    stubs = check_hardware(trigger, device, meter)
    if stubs and not a.dry:
        raise SystemExit(
            "Refusing to start: %s %s still the stub implementation%s shipped "
            "in\nclient.py, so this run would record zero joules against every "
            "call and\nlook exactly like a real campaign afterwards.\n\n"
            "Implement the hardware interfaces, or pass --dry for a structural "
            "rehearsal."
            % (", ".join(stubs), "is" if len(stubs) == 1 else "are",
               "" if len(stubs) == 1 else "s")
        )

    run_id, out_dir = resolve_run(a.resume, a.dry)

    llama = client.LlamaClient(None, trigger=trigger, device=device, meter=meter,
                               host=a.host, port=a.port, run_id=run_id)
    if not llama.health():
        raise SystemExit(
            "No llama-server answering at http://%s:%d.\nStart it with "
            "scripts/serve_dev.sh, which reads CTX_SIZE and LLAMA_FLAGS from "
            "config.py." % (a.host, a.port))

    runner = Runner(llama, ROOT, out_dir=out_dir, run_id=run_id)
    banner(runner, a.dry, a.host, a.port)

    try:
        try:
            executed = runner.run()
        finally:
            for instrument in (trigger, meter, device):
                instrument.close()
    except client.ThermalEmergency as exc:
        # Deliberately its own branch, not folded into a generic except
        # below client.py's module docstring's own rule that faults are
        # recorded and a run continues past them. This is the one condition
        # meant to stop a run outright, so it gets its own loud message, its
        # own durable on-disk record (terminal scrollback is not enough for
        # an operator who was not watching an unattended multi-day
        # campaign), and its own exit code, distinct from every other exit
        # path in this script, so a launcher or monitoring script watching
        # the process exit status can tell "the campaign finished or was
        # interrupted" (0) apart from "the campaign stopped itself to
        # protect the hardware, do not just restart it" (3).
        marker = runner.out / "THERMAL_EMERGENCY.json"
        try:
            marker.write_text(json.dumps({
                "run_id": run_id,
                "stopped_utc": datetime.now(timezone.utc).isoformat(),
                "message": str(exc),
            }, indent=2), encoding="utf-8")
        except OSError:
            pass  # the stderr message below is the fallback record
        # A plain string SystemExit always exits 1 in Python regardless of
        # its message, which would make this indistinguishable from every
        # other guard failure in this script; the message and the exit code
        # are set separately here so the distinct-exit-code claim above is
        # actually true, not just asserted in a comment.
        sys.stderr.write(
            "\n%s\n\nWrote %s.\nDo not resume this run until the cooling "
            "problem is understood and fixed; resuming\nwith --resume %s "
            "picks the campaign back up exactly where this left off,\nand "
            "will run straight back into the same limit if nothing about "
            "the physical\nsetup has changed.\n" % (exc, marker, run_id))
        raise SystemExit(3) from None

    print("\n%d tasks executed. Resume with:\n  python3 scripts/run_campaign.py "
          "--resume %s%s" % (executed, run_id, " --dry" if a.dry else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
