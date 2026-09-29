"""Planted faults (rule F): changes inside the network model, their CM, FM
and PM traces, and the evaluation-only answers.

Every fault is a change inside the model (rule F1), never a painted-on
counter pattern:

- F1a tilt changed by mistake: the electrical tilt is raised (uptilt), the
  cell overshoots; right answer: restore the tilt.
- F1b missing neighbour: the strongest neighbour relation is deleted;
  right answer: add the neighbour back.
- F1c power reduced after maintenance; right answer: restore the power.
- F1d capacity: a traffic surge in the population layer around the cell;
  right answer: a load-balancing offset or a capacity note.
- F1e external interference: an uplink interference source near the
  cell; no parameter fix, a field visit.
- F1f outage: the cell is down for hours; no parameter fix, alarm-driven.
"""
