Control loops
=============

A control is a closed loop that holds something at a target by switching devices: a
conservatory at a temperature, using dumb heaters on smart plugs. It reads its inputs from
the data this installation already collects, runs a PID, and picks rungs from a ladder of
device combinations.

This file is the reference for writing one. The README covers whether controls run at all;
`architecture/mcp-server.md` covers how the MCP layer is built.

**Everything here is also available at runtime from the `get_control_schema` MCP tool**,
which builds it from the same constants this file is checked against - so a model composing
a control is not guessing, and is not reading something that has drifted from the code.

Switching them on
-----------------

Two separate opt-ins, and neither is a collector's `mcp_read_write` flag:

```yaml
controls:
  enabled: true      # run the stored controls
  mcp_write: true    # let an MCP client write them for you
```

`enabled` runs the controls you have. Controls are off unless you switch them on, because a
control actuates devices with nobody watching. Wanting a heating loop is not the same as
granting a connected model device-write access, and one setting governing both would force
anyone wanting the first to accept the second.

`mcp_write` lets a connected model create, change and delete controls, effective
immediately. It is a bigger grant than it looks, and bigger than `mcp_read_write`: that
permits an action now, where this permits a *standing rule* that keeps acting, unattended,
for as long as it exists.

Neither implies the other, and both are exactly `true` - a quoted `"true"` is a string, and
`--check-config` will say so rather than leaving you with a switch that reads as off.

**With `mcp_write` off, an assistant can still help.** The read tools stay available, so it
can describe your controls, explain one, and compose a document for you to save by hand - it
is told where to tell you to put it. What it cannot do is save it for you. If you find an
assistant repeatedly handing you YAML instead of installing it, this switch is why.

Where they live
---------------

One YAML document per control, under the state directory - `/var/lib/send-to-influx/controls`
on a packaged install, beside `settings.yaml` in a source checkout. They are written by the
running service, and by an MCP client on its behalf where `mcp_write` permits it, though
hand-editing works and `--check-config` will tell you if you got it wrong.

**A change made through the MCP tools** is picked up without restarting the service: the
supervisor reconciles a control with its document, so a saved edit restarts that loop, a
deleted document stops it and makes its devices safe, and a document that will not parse
leaves the running control alone rather than killing it over a half-finished edit.

**A file edited by hand needs a restart.** Nothing watches this directory; the reconcile is
triggered by the write tools, not by the filesystem.

The document
------------

| Key | Required | What it is |
| --- | --- | --- |
| `active_period` | no | {from, to, end_state}: a daily wall-clock window in the control's own timezone |
| `devices` | yes | name -> {source, device, instance, min_transition_seconds}: what the control switches |
| `enable_when` | no | a rule gating actuation; the control acts only while it evaluates non-zero |
| `enabled` | no | true or false; false keeps the document without running the loop |
| `inputs` | yes | name -> {source, field, instance, max_age}: the readings the rules may use |
| `name` | no | the control's own name, which must match the file it is stored as |
| `output` | yes | cycle_seconds, min_transition_seconds, an optional max_level rule, and the stage ladder |
| `parameters` | no | constants the rules may read, such as a target temperature, adjustable at runtime |
| `pid` | yes | the loop itself: input and setpoint rules, and the kp, ki and kd gains |
| `safe_state` | no | what the devices do at startup, on failure and at shutdown |
| `timezone` | no | an IANA zone name for the active period; absent means this machine's local time |

A key the store does not permit is refused rather than ignored: a mistyped key that was
quietly dropped would leave you looking at a setting you believe is in force and is not.

A required key that is present with nothing under it - `inputs:` with the block unindented
beneath it - is refused too, and says so specifically, because that is what the mistake
actually looks like.

The rule language
-----------------

These slots hold an expression:

| Slot | |
| --- | --- |
| `pid.setpoint` | required |
| `pid.input` | required |
| `output.max_level` | optional |
| `enable_when` | optional |

Rules are **parsed, never evaluated as code**. The grammar is arithmetic over numbers and
nothing else: no strings, no attribute access, no indexing, no assignment. There is no path
from a rule to an object, an import or the filesystem, because the language cannot express
one.

A rule may read the keys of `inputs` and `parameters`, and nothing else. A name nothing
declares is refused when the document is validated, not discovered at three in the morning.

| Function | |
| --- | --- |
| `abs` | 1 to 1 arguments |
| `clamp` | 3 to 3 arguments |
| `if` | 3 to 3 arguments |
| `max` | 2 or more arguments |
| `min` | 2 or more arguments |

Operators: `<=` `>=` `==` `!=` `<` `>` `+` `-` `*` `/`, and the keywords `and`, `not`, `or`.

Five things that are not obvious:

* **Truth is a number.** A comparison yields 1 or 0, `if` treats non-zero as true, and a
  gate acts while its rule evaluates non-zero. `and` and `or` return 1 or 0 rather than one
  of their operands, so a non-truth value cannot leak out of a gate into a sum.
* **`and`, `or` and `if` short-circuit**, so a rule can guard its own arithmetic:
  `divisor != 0 and total / divisor > 5` never divides by zero.
* **Comparisons do not chain.** Python reads `a < b < c` as a chain and C reads it as
  `(a < b) < c`. Both are defensible and they disagree, so it is refused and the message
  names the `and` form to write instead.
* **Precedence is Python's**: `or`, `and`, `not`, comparison, `+ -`, `* /`, unary minus.
* **A number may not run straight into a name.** Write `1 and 2`, not `1and 2`. The cost of
  making `1e` report a broken number literal rather than a stray identifier.

A rule is at most 2000 characters and 40 levels of nesting.
Both are far beyond any real rule; they exist because a rule arrives from an MCP client and
unbounded nesting exhausts the interpreter's stack, which escapes as a crash rather than as
a report about a bad rule.

Safe state, the active period and enabling
------------------------------------------

Three settings decide what a control does when it is not actively holding a target, and they
are separate because they answer different questions:

| Setting | When it applies |
| --- | --- |
| `safe_state` | at startup, on failure, and at shutdown |
| `active_period.end_state` | when the control's daily window closes |
| `enable_when` | a rule gating actuation while everything else is running |

The safe states are `unenergised` and `leave_unchanged`.

`unenergised` is the default and switches every device the control owns off **by name**,
rather than meaning "the lowest stage" - so it does not depend on a zero stage having been
declared correctly.

`leave_unchanged` is the opt-out and means exactly that: the devices keep whatever state they
were in. **It also removes the startup assertion**, which is what would otherwise clear the
mess a crash left, so after a SIGKILL or a power cut the device stays where it was and
nothing will correct it. The right trade for a light you do not want going out because a
server rebooted, and the wrong one for a heater.

An active period is a wall-clock window in the control's own timezone, and it follows
daylight saving the way a wall clock does: a window inside the hour the clocks skip does not
happen that day, and one inside the hour they repeat happens twice.

A control actuates when `enabled` **and** inside the active period **and** `enable_when`
holds. Any falling edge applies the end state and holds the loop; any rising edge resumes it
without the integral jumping.

The stage ladder
----------------

Two independently switchable heaters give a ladder of discrete levels, not a continuous
output. The PID produces a demand on the level scale, and the cycle window is split between
the two stages bracketing it: a demand of 1237 with rungs at 750 and 1500 spends 65% of the
window at 1500 and 35% at 750.

* Stages sort by declared `level`, and "the next stage up" is by level rather than list
  order.
* Among stages of equal level the earliest declared wins. Declaration order is how you say
  which element should do the steady-state work: the far heater first, so the near one,
  sitting next to the sensor, only trims.
* **Every stage must assign every device the control owns.** A stage saying nothing about a
  device is not a stage turning it off, and this is refused - an operator reading the ladder
  would assume it was off, which is how a heater stays on.
* `level` must be a number. `true` would validate and then sort as 1, inserting a rung.
* `min_transition_seconds` protects hardware that objects to frequent switching - a relay, a
  compressor - rather than smoothing the loop, which is the PID's job. Tens of seconds is the
  usual range. It is enforced per device, and only for devices that actually change between the
  two rungs. **It may not exceed `cycle_seconds`** and is refused if it does: the limit binds
  within a window, not across the boundary between them, so a longer one would be broken at
  every boundary anyway.
* `max_level` caps the **ladder**, not the demand. Capping the demand still proportions
  between rungs above the cap; capping the ladder does not.

A worked example
----------------

This is the example the project tests itself against, and CI refuses it if it stops being
valid - so it is known to work rather than known to have been checked once. It is what
`get_control_schema` hands out.

```yaml
name: conservatory
enabled: true
timezone: Europe/London
parameters:
  target: 18.0
inputs:
  inside:
    source: hue
    field: temperature_conservatory
    instance: bridge1
    max_age: 900
  dew:
    source: openmeteo
    field: dew_point_2m
    max_age: 1800
  outside:
    source: openmeteo
    field: temperature_2m
    max_age: 1800
  grid_co2:
    source: carbonintensity
    field: intensity_actual
    max_age: 3600
pid:
  input: inside
  setpoint: max(target, dew + 5)
  kp: 12.0
  ki: 0.02
  kd: 0.0
output:
  cycle_seconds: 900
  min_transition_seconds: 60
  max_level: if(grid_co2 > 300, 750, 2250)
  stages:
  - level: 0
    set:
      heater_far: false
      heater_near: false
  - level: 750
    set:
      heater_far: true
      heater_near: false
  - level: 1500
    set:
      heater_far: true
      heater_near: true
devices:
  heater_far:
    source: hue
    device: Conservatory heater far
    min_transition_seconds: 180
  heater_near:
    source: hue
    device: Conservatory heater near
enable_when: outside < 15
safe_state: unenergised
active_period:
  from: '23:35'
  to: 05:25
  end_state: unenergised
```

What is checked, and when
-------------------------

`--check-config` validates every stored control and reports **all** of their problems at
once, not the first, because fixing one typo per run is not a review. It checks three things:

* **the shape** - unknown keys, missing or empty required sections, a stage that omits a
  device, a device referenced by no stage;
* **the rules** - every slot parses, and every name it reads is declared;
* **the sources** - every source named is one this build collects from, and every source in
  `devices` can actually switch a device on and off.

Left to a control's startup: whether the device exists on the bridge, and whether it has a
capability a stage asks for. Those need the far end, and they fail that control only.

Reading them over MCP
---------------------

With `controls.enabled` and the MCP server both on, three read-only tools appear:
`list_controls` (names, enabled, devices, cycle length, and whether each process is running),
`get_control` (one document as stored), and `get_control_schema` (this format). They are not
behind any write flag: a control document holds no secrets, and being able to ask what is
being controlled and whether it is running should not require granting the ability to change
it.

Add `controls.mcp_write` and three more appear: `save_control` (create or replace a document,
validated before anything is written), `set_control_enabled` (turn one on or off without
rewriting it) and `delete_control`. Each takes effect without restarting the service. With the
flag off, those three are not registered at all rather than present and refusing, and
`get_control_schema` says so and names the setting - so an assistant can tell "this install
has not enabled writing" from "this build cannot", and tell you which.

With the subsystem switched off they are not registered at all, so a connected model does not
see tools it cannot use.
