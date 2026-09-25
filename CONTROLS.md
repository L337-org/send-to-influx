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
| `devices` | yes | name -> {source, device, instance, min_transition_seconds, parameter}: what the control drives |
| `enable_when` | no | a rule gating actuation; the control acts only while it evaluates non-zero |
| `enabled` | no | true or false; false keeps the document and runs no process for it at all |
| `inputs` | yes | name -> {source, field, instance, max_age}: the readings the rules may use |
| `name` | no | the control's own name, which must match the file it is stored as |
| `output` | yes | cycle_seconds, min_transition_seconds, an optional max_level rule, and the stage ladder |
| `parameters` | no | constants the rules may read, such as a target temperature, adjustable at runtime |
| `pid` | yes | the loop itself: input and setpoint rules, and the kp, ki and kd gains |
| `safe_state` | no | unenergised, energised or leave_unchanged: what the devices do at startup, on failure and at shutdown |
| `timezone` | no | an IANA zone name for the active period; absent means this machine's local time |

A key the store does not permit is refused rather than ignored, in the nested sections as
well as at the top level: a mistyped key that was quietly dropped would leave you looking at
a setting you believe is in force and is not. The exceptions are the two places whose keys
are yours to choose - `parameters`, and the device names inside a stage's `set`.

Every source a control names needs a settings section on the machine running it, for its
devices and its inputs alike: an input is read through a source handler too, which resolves
the database to query from that source's own section.

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

The safe states are `unenergised`, `energised` and `leave_unchanged`.

`unenergised` is the default and switches every device the control owns off **by name**,
rather than meaning "the lowest stage" - so it does not depend on a zero stage having been
declared correctly.

`energised` is its mirror and switches them all on, named the same way. It is there because
the device is not necessarily a heater: for a circulation pump whose stopping lets a boiler
overheat, a valve held open by power, frost protection, or an extractor that must not stop,
off is the dangerous state and on is the safe one. **The consequence to weigh is that the
device then keeps drawing power with nothing supervising it** - the control is not running,
which is why its safe state applied - so it holds until somebody or something else
intervenes. That is the right trade for a pump and the wrong one for a heater, which is why
`unenergised` remains the default and this is opt-in.

`leave_unchanged` is the opt-out and means exactly that: the devices keep whatever state they
were in. **It also removes the startup assertion**, which is what would otherwise clear the
mess a crash left, so after a SIGKILL or a power cut the device stays where it was and
nothing will correct it. The right trade for a light you do not want going out because a
server rebooted, and the wrong one for a heater.

All three apply to `active_period.end_state` as well, so a control can hold a room at
temperature overnight and leave its pump running when the window closes.

**A number is a state too**, for controls with driven devices: `safe_state: 40` leaves a lamp
at 40%, and any switched device in the same control on, since a value above zero means on for
something that has only two of them. Zero means off for both. `unenergised` is 0 and false,
`energised` is 100 and true - and `energised` is refused for a device driven by something
without a full scale, such as a colour temperature, where 100 would be a nonsense rather than
a bright light; give the value outright instead.

**A control that starts outside its active period starts in its `end_state`, not its
`safe_state`.** The two answer different questions - `safe_state` is "something is wrong, or
nothing is known yet", `end_state` is "the control is deliberately not acting" - and a
process starting outside its window is the second. Without this a pump with
`safe_state: energised` and a window of 23:30 to 05:30, restarted at noon, would run all
afternoon in its failure state and nothing would correct it until the window had opened and
closed again. `enable_when` does not take part, because answering it needs a sensor read and
the whole point of the startup assertion is that it happens before anything is read; the
active period needs only the clock. On the way out the `safe_state` applies whatever the
clock says, because a process that is ending leaves nothing supervising the devices.

An active period is a wall-clock window in the control's own timezone, and it follows
daylight saving the way a wall clock does: a window inside the hour the clocks skip does not
happen that day, and one inside the hour they repeat happens twice.

**Quote the times.** YAML reads a colon-separated value with no leading zero as sexagesimal,
so an unquoted `23:35` is the number 1415 while `05:25` survives as the string it looks like.
Both are refused, and the error says so, but the rule is not one anyone should have to carry -
so quote every boundary and the question never arises.

A disabled control has no process. That is not the same as a running one held back by its
gate: a control asserts its safe state before its first cycle, so a disabled document that
was started would command its devices off - and `save_control` deliberately stores a document
clashing with a running control *disabled* rather than refusing it, so a disabled replacement
would switch off the live control's heater on arrival. Enabling one starts it without a
restart; disabling a running one stops it and puts its devices in their safe state.

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
  compressor - rather than smoothing the loop, which is the PID's job. It is enforced per
  device, and only for devices that actually change between the two rungs.
* **It may be longer than `cycle_seconds`**, and the two are unrelated settings: how often the
  loop recomputes, and how often a given piece of hardware may be switched. A device still
  inside its minimum is held where it is, and the window is planned from the rungs that leave
  it there - so a fast loop can drive a responsive device while a slow one beside it is
  protected. The record of when each device last moved is kept in the state directory, one
  file per control, and survives a restart, because the supervisor restarts a control on every
  document edit and a guarantee that lapsed there would be no guarantee at all.
* **A safe state overrides it in both directions.** Going in, the safe state is commanded
  whatever the clock says. Coming out, the control may act immediately rather than waiting a
  full minimum - otherwise a heater forced off by a transient fault would sit there long after
  the fault had cleared, which is the setting protecting the hardware from the safety
  mechanism. The next ordinary command restores the normal rule.
* `max_level` caps the **ladder**, not the demand. Capping the demand still proportions
  between rungs above the cap; capping the ladder does not.

Choosing the gains
------------------

`level` is a scale you choose - watts, percent, anything - and **the PID's gains are in that
scale per unit of input**. Get this wrong and the loop looks broken rather than mistuned: a
`kp` three orders of magnitude too small delivers nothing at any realistic error, and the
integral takes hours to make up the difference, which reads as a control that does not work.

The starting point is one division:

```
kp = top rung level / the error at which you want full output
```

For a ladder topping out at 1500 and full output three degrees below setpoint, `kp` is 500.
The shipped examples are all set this way, and the comment beside each says which error it
was chosen for, so scaling one to your own plant is a matter of changing that number.

`ki` then trims the residual offset that proportional action alone always leaves. It is
applied per second, so a useful starting value is roughly `kp / 3000` for a slow plant like a
room and more for something small and fast; the output limits follow the ladder, so it cannot
wind up beyond what the devices can deliver. `kd` is usually best left at zero, because a
temperature or light reading is noisy and differentiating noise amplifies it.

Switched devices and driven ones
--------------------------------

A device with no `parameter` is switched on and off, and its stage entries are `true` or
`false`. A device that names one is **set to a value**, and its stage entries are numbers on
that parameter's own scale:

```yaml
devices:
  lamp: {source: hue, device: Office Lamp, parameter: brightness_pct}
output:
  stages:
  - {level: 0, set: {lamp: 0}}
  - {level: 1000, set: {lamp: 100}}
```

The reason the two behave differently is that a heater has no middle setting and a dimmer
does. A switched device is **time-proportioned**: the window is split between two rungs so
that it averages out at the demand. A driven device takes the value the ladder describes *at*
the demand and holds it for the whole window, because proportioning a dimmer would be flicker
rather than control. So the ladder is a set of rungs for one and a transfer curve for the
other, and one control can hold both, each driven by the method its own hardware supports.

`min_transition_seconds` means the same thing in both cases once you read it as "how often
this device may change": for a switch that is how often it may flip, and for a dimmer how
often it is adjusted. A driven device inside its minimum is commanded the value it already
has, and it never constrains how the window is split, because its value is the same in both
halves.

Which parameters exist is a property of the source. Hue drives `brightness_pct` and
`color_temp_k`; `--check-config` refuses a parameter the source does not know, and whether a
*particular* lamp is dimmable is checked against the bridge when the control first commands
it, where the error can name the device.

Worked examples
---------------

Three complete documents, one per situation, and `get_control_schema` hands out all
three. CI validates every one of them, so they are known to work rather than known to
have been checked once.

**Copy one whole rather than taking values from several.** These are templates, not
illustrations: every number in them is one you would actually run. Mixing them is how a
control ends up with a transition minimum that belonged to neither - which is a real
incident, not a hypothetical one.

### Normal

The usual case: devices that can be switched as often as the loop likes, and one
transition minimum covering all of them. Start here.

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
  kp: 500.0
  ki: 0.15
  kd: 0.0
output:
  cycle_seconds: 300
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
  heater_near:
    source: hue
    device: Conservatory heater near
enable_when: outside < 15
safe_state: unenergised
active_period:
  from: '23:35'
  to: '05:25'
  end_state: unenergised
```

### Slow response

One device's effect takes longer to show up at the sensor than another's - a heater
across the room from it, or a larger load - so it should be held while the nearer one
trims. `heater_far` gets its own longer minimum, and that binds **only at the rungs
where `heater_far` itself changes**: at a demand between 0 and 750 the window collapses
onto one rung rather than switching it twice, while a demand between 750 and 1500 still
splits at `heater_near`'s own 60 seconds. That is the whole reason a per-device minimum
is worth having, and the only example here that carries one.

```yaml
name: conservatory_staged
enabled: true
timezone: Europe/London
parameters:
  target: 18.0
inputs:
  inside:
    source: hue
    field: temperature_conservatory
    max_age: 900
  outside:
    source: openmeteo
    field: temperature_2m
    max_age: 1800
pid:
  input: inside
  setpoint: target
  kp: 500.0
  ki: 0.15
  kd: 0.0
output:
  cycle_seconds: 300
  min_transition_seconds: 60
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
    min_transition_seconds: 120
  heater_near:
    source: hue
    device: Conservatory heater near
enable_when: outside < 15
safe_state: unenergised
```

### Dimming

A device set to a value rather than switched. The lamp holds the brightness the ladder
describes at the demand, adjusted at most every 30 seconds. Worth knowing before tuning one:
a lamp driven by a sensor that can see the lamp is a feedback loop, so the sensor wants to be
reading the ambient you care about rather than the lamp itself.

```yaml
name: office_lamp
enabled: true
timezone: Europe/London
parameters:
  target: 300.0
inputs:
  brightness:
    source: hue
    field: light_level_office
    max_age: 300
pid:
  input: brightness
  setpoint: target
  kp: 5.0
  ki: 0.02
  kd: 0.0
output:
  cycle_seconds: 30
  min_transition_seconds: 30
  stages:
  - level: 0
    set:
      lamp: 0
  - level: 1000
    set:
      lamp: 100
devices:
  lamp:
    source: hue
    device: Office Lamp
    parameter: brightness_pct
safe_state: unenergised
```

### Fast adjustment

A small thermal mass and a device with nothing to protect. The loop recomputes every
minute and the window splits as finely as ten seconds.

```yaml
name: propagator
enabled: true
timezone: Europe/London
parameters:
  target: 21.0
inputs:
  tray:
    source: hue
    field: temperature_propagator
    max_age: 300
pid:
  input: tray
  setpoint: target
  kp: 1000.0
  ki: 2.0
  kd: 0.0
output:
  cycle_seconds: 60
  min_transition_seconds: 10
  stages:
  - level: 0
    set:
      mat: false
  - level: 1000
    set:
      mat: true
devices:
  mat:
    source: hue
    device: Propagator mat
safe_state: unenergised
```

Watching one run
----------------

A control says nothing during a healthy cycle, which is right for a service that runs for
months and wrong when you are tuning one. Run with `-v`, or set `loglevel: DEBUG`, and each
cycle records what it read, what it was chasing, what the PID asked for, and what the ladder
could actually give it:

```
input=16.000 setpoint=20.000 demand=400.0 (p=400.0 i=0.0 d=-0.0) plan=level 0 for 140s, level 750 for 160s
```

`held=` appears when a device is inside its `min_transition_seconds` and the window had to be
planned around it. That is the line worth knowing about, because a held device makes a control
look like it is doing the opposite of what it was told - a demand of 150 commanding level 750
for the whole window is correct when the far heater may not switch off yet, and inexplicable
without it.

What a control remembers
------------------------

Each control keeps a small file in the state directory - `transitions/<name>.json` - holding
what it had done when it last ran. Two halves, both there for the same reason: a restart
should not cost the control what it already knew.

The **device half** records what each device was last commanded to and when, which is what
makes `min_transition_seconds` survive a restart. Without it, a heater switched off a second
before a service restart could be switched on again immediately, because the process that
knew about it had gone.

The **loop half** records the PID's integral. That is the part a slow plant spends a long
time earning, and rebuilding it from nothing is why a restarted control can sit below target
for an hour having already learned the answer once. It is put back only when the stored
memory still describes the present, on two tests: the gains, ladder and cycle must be
unchanged, because an integral is in the output's units and means something else under a
different tuning; and it must be recent, within a few cycles, because a machine that has been
down long enough for the room to change should look at the room rather than at what it
remembered. Failing either, the control starts afresh - which is simply what it always did.
`--verbose` says which happened.

The file is written whenever a device actually changes, and once per cycle for the loop
half. Deleting it costs nothing but the memory; the control rebuilds both.

What is checked, and when
-------------------------

`--check-config` validates every stored control and reports **all** of their problems at
once, not the first, because fixing one typo per run is not a review. It checks three things:

* **the shape** - unknown keys at any level, missing or empty required sections, a stage
  that omits a device, a device referenced by no stage;
* **the rules** - every slot parses, and every name it reads is declared;
* **the sources** - every source named is one this build collects from, has a settings
  section on this machine, and, in `devices`, can actually switch a device on and off.

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
