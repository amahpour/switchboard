# Whiteboard recipes

One worked example for each kind of picture. Ids are the ones each call returns; the examples
name them `a`, `b`, `c` for brevity.

## A flow or dependency map: `draw_mermaid`

Anything that is a graph. Subgraphs make the groups.

```
flowchart LR
  subgraph Inputs
    spec[Spec] --> parser[Parser]
  end
  subgraph Change
    parser --> engine[Engine rewrite]
  end
  engine --> gate{Tests pass?}
  gate -->|yes| ship[Release]
  gate -->|no| engine
```

Then `read`, fix every warning, and answer with one line that points at the board.

## An architecture: frames and arrows

One frame per layer or service group, at most 8 shapes in each.

1. `frame` text="Clients", then `frame` text="Broker", then `frame` text="Storage".
2. `box` text="Web UI" to=<Clients>, `box` text="Agents" to=<Clients>. The frame grows to fit.
3. `connect` between neighbours; one `connect` from a shape in Clients to one in Broker with
   `style="arrow-type=elbow"` for the hand-off between groups.

## A sequence, as a storyboard

One frame per step, titled with the step ("1. A person posts"), left to right. Inside a frame,
two or three boxes and one or two arrows. Add a `text` heading with the one thing to notice.
People play the frames in order.

## A comparison

A grid of notes: a header row of `text` actions (`style="font-size=l"`), then one row of `note`s
per option, columns 300 apart and rows 140 apart. Colour by verdict, and add a legend note:

```
note text="Option A" x=0 y=0 style="background=green"
note text="Option B" x=300 y=0 style="background=red"
note text="green: fits  red: does not" x=600 y=0 style="background=gray"
```

## A timeline

A row of boxes 300 apart, joined by arrows with no labels, and a `text` caption under each:

```
box text="Design" x=0 y=0
box text="Build" x=300 y=0
connect id=<Design> to=<Build>
text text="week 1" x=0 y=100 style="font-size=s"
```

## Fixing warnings

`overlap`: `move` one shape 300 right or 140 down. `label may overflow`: shorten it, or `resize`
wider. `outside its frame`: `resize` the frame, or `move` the shape in. `arrow passes through`:
`move` the blocking shape, or connect through an elbow (`style="arrow-type=elbow"`).
`arrow labels overlap`: drop one label.
