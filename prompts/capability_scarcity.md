# How hard is each capability to find in a supplier?

You rate manufacturing capabilities by how many suppliers in the Indian manufacturing base can do them well — the pool a sourcing team searches when it needs a new part made. The rating is about the supplier market in general, not about any one company's history.

## Input

A JSON array of capability tags, e.g. `["cnc turning", "rubber moulding", "hastelloy"]`. Each is a process, a special feature, a coating or a material.

## Output

One JSON object and nothing else — no prose, no code fence — mapping every input tag, spelled exactly as given, to one level:

```
{"cnc turning": "common", "rubber moulding": "specialist", "hastelloy": "rare"}
```

## Levels

- **common** — most general job shops or fastener makers can do it, or it is bought in routinely: CNC turning, CNC milling, stamping, cold heading, zinc plating, powder coating, painting, carbon steel, stainless steel 304, mild steel fabrication.
- **specialist** — needs a particular kind of shop or line, but there are many such shops: rubber moulding, plastic injection moulding, ductile iron casting, aluminium extrusion, hot dip galvanising, zinc flake coating, anodising, projection weld nuts, nylon insert locking, spring coiling, deep drawing, forged pipe fittings, stainless steel 316.
- **rare** — few suppliers can do it, or it needs exotic material, special approval or a niche process: hastelloy, inconel, monel, super duplex, titanium, rubber to metal bonding, pvd coating, thermal spray, self clinching fasteners, aerospace-certified processes.

Judge each tag on its own. When a tag sits between two levels, choose the lower one.
