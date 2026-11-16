"""Map evidence for the independent grader. Distances/speeds use metres and m/s."""
import math
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict

from PIL import Image, ImageDraw


def points(shape):
    return [tuple(map(float, pair.split(",")[:2])) for pair in shape.split()]


class Network:
    def __init__(self, path):
        root = ET.parse(path).getroot()
        if root.tag != "net":
            raise ValueError("expected a SUMO net")
        location = root.find("location")
        location = {} if location is None else location.attrib
        self.proj = location.get("projParameter", "!")
        ox, oy = points(location.get("netOffset", "0,0"))[0]
        self.nodes = {}
        self.types = {}
        for node in root.findall("junction"):
            if node.get("type") != "internal":
                key = node.attrib["id"]
                self.nodes[key] = (float(node.attrib["x"]) - ox, float(node.attrib["y"]) - oy)
                self.types[key] = node.get("type")
        self.edges = {}
        self.lines = []
        self.links = set()
        self.endpoints = {}
        for edge in root.findall("edge"):
            if edge.get("function") in {"internal", "crossing", "walkingarea"}:
                continue
            lanes = edge.findall("lane")
            if not lanes or not all(edge.get(k) in self.nodes for k in ("from", "to")):
                continue
            origin, destination = edge.get("from"), edge.get("to")
            key = edge.attrib["id"]
            self.edges[key] = [float(lane.attrib["speed"]) for lane in lanes]
            self.endpoints[key] = origin, destination
            for lane in lanes:
                line = [(x - ox, y - oy) for x, y in points(lane.get("shape", ""))]
                if len(line) < 2:
                    line = [self.nodes[origin], self.nodes[destination]]
                self.lines.append(line)
        for turn in root.findall("connection"):
            a, b = turn.get("from"), turn.get("to")
            if a in self.edges and b in self.edges:
                self.links.add((a, turn.get("fromLane"), b, turn.get("toLane")))
        if not self.lines:
            raise ValueError("no external road geometry")
        self.location = location

    def stats(self):
        return {"directed_roads": len(self.edges), "lanes": sum(map(len, self.edges.values())),
                "lane_connections": len(self.links), "junction_types": dict(Counter(self.types.values())),
                "projection": self.proj, "original_bounds": self.location.get("origBoundary")}

    def road_rows(self):
        def describe(edge):
            a, b = self.endpoints[edge]
            start, end = self.nodes[a], self.nodes[b]
            return {"from": start, "to": end, "from_type": self.types[a], "to_type": self.types[b],
                    "junction_distance_m": round(math.dist(start, end), 2), "lane_speeds_mps": self.edges[edge]}
        return [describe(e) for e in sorted(self.edges)[:80]]


def edit_diff(old, new, target):
    # Endpoint geometry fallback for a renamed edge. Ambiguous parallel edges stay unmatched.
    groups = defaultdict(list)
    def signature(net, key):
        return tuple(tuple(round(v, 2) for v in net.nodes[n]) for n in net.endpoints[key])
    for k in new.edges:
        groups[signature(new, k)].append(k)
    pairs = {}
    for k in old.edges:
        matches = groups.get(signature(old, k), [])
        if k in matches:
            pairs[k] = k
        elif len(matches) == 1:
            pairs[k] = matches[0]
    used = Counter(pairs.values())
    pairs = {k: v for k, v in pairs.items() if used[v] == 1}
    changes = [{"original_edge": k, "old_mps": old.edges[k], "new_mps": new.edges[v]}
               for k, v in pairs.items() if old.edges[k] != new.edges[v]]
    inverse = {v: k for k, v in pairs.items()}
    turns = {(inverse.get(a), i, inverse.get(b), j) for a, i, b, j in new.links}
    return {"target_before_mps": old.edges[target], "target_after_mps": new.edges.get(pairs.get(target)),
            "speed_changes_count": len(changes), "speed_changes_first_40": changes[:40],
            "unmatched_original_edges": len(old.edges) - len(pairs),
            "unmatched_output_edges": len(new.edges) - len(pairs),
            "lost_lane_connections": len(old.links - turns), "gained_lane_connections": len(turns - old.links)}


def real_alignment(ref, candidate):
    """OSM pilot uses metric projected coordinates. SUMO offsets were already removed."""
    projected = ref.proj.startswith("+proj=") and "+proj=longlat" not in ref.proj
    if projected and ref.proj == candidate.proj:
        return True
    if projected and candidate.proj not in ("!", "-", ".", ""):
        try:
            from pyproj import Transformer
            transform = Transformer.from_crs(candidate.proj, ref.proj, always_xy=True)
            def xy(p):
                result = transform.transform(*p, errcheck=True)
                if not all(math.isfinite(v) for v in result):
                    raise ValueError("non-finite coordinates")
                return result
            nodes = {k: xy(p) for k, p in candidate.nodes.items()}
            lines = [[xy(p) for p in line] for line in candidate.lines]
            candidate.nodes, candidate.lines = nodes, lines
            return True
        except (ImportError, ValueError, RuntimeError):
            pass
    return False


def extent(lines):
    vertices = [p for line in lines for p in line]
    return (min(p[0] for p in vertices), min(p[1] for p in vertices),
            max(p[0] for p in vertices), max(p[1] for p in vertices))


def render(lines, bounds):
    # The real-map panels both use the reference viewport, regardless of predicted bounds.
    w = 640
    xmin, ymin, xmax, ymax = bounds
    scale = (w - 36) / max(xmax - xmin, ymax - ymin, 1)
    cx, cy = (xmin + xmax) / 2, (ymin + ymax) / 2
    image = Image.new("RGB", (w, w), "white")
    pen = ImageDraw.Draw(image)
    for line in lines:
        pixels = [(w / 2 + (x - cx) * scale, w / 2 - (y - cy) * scale) for x, y in line]
        # Extreme out-of-viewport maps can exceed Pillow's C coordinate range.
        pixels = [(max(-1e7, min(1e7, x)), max(-1e7, min(1e7, y))) for x, y in pixels]
        pen.line(pixels, fill="#244f73", width=2)
    pen.text((8, 8), "N ^", fill="black")
    return image


def grid_coverage(lines, cell=5):
    # Approximate geometric coverage, independent of SUMO edge IDs and segmentation.
    cells = set()
    for line in lines:
        for a, b in zip(line, line[1:]):
            n = max(1, math.ceil(math.dist(a, b) / (cell / 2)))
            if n > 100000:
                raise ValueError("road segment too large for pilot grid")
            for i in range(n + 1):
                x, y = (a[j] + (b[j] - a[j]) * i / n for j in (0, 1))
                cells.add((math.floor(x / cell), math.floor(y / cell)))
    return cells


def route_probes(ref, candidate, limit=12):
    # Probe legal passenger-independent edge-to-edge turns; do not assume a fully connected map.
    def successors(net):
        adjacency = defaultdict(set)
        for a, _, b, _ in net.links:
            adjacency[a].add(b)
        return adjacency
    def reachable(adj, start):
        seen, pending = {start}, [start]
        while pending:
            for nxt in adj[pending.pop()] - seen:
                seen.add(nxt)
                pending.append(nxt)
        return seen
    def ends(net, edge):
        return [net.nodes[k] for k in net.endpoints[edge]]
    edges = sorted(ref.edges)
    probes = [edges[i] for i in sorted({round(j * (len(edges)-1) / (limit-1)) for j in range(limit)})]
    mapped = {}
    for e in probes:
        a, b = ends(ref, e)
        def error(edge):
            x, y = ends(candidate, edge)
            return max(math.dist(a, x), math.dist(b, y))
        best = min(candidate.edges, key=error)
        if error(best) <= 10:
            mapped[e] = best
    base, output = successors(ref), successors(candidate)
    same = positives = maintained = 0
    for a in probes:
        truth = reachable(base, a)
        reached = reachable(output, mapped[a]) if a in mapped else set()
        for b in probes:
            if b == a:
                continue
            expected = b in truth
            positives += expected
            if a in mapped and b in mapped:
                observed = mapped[b] in reached
                same += observed == expected
                maintained += observed and expected
    return {"probe_ordered_pairs": len(probes)*(len(probes)-1), "agreeing_pairs": same,
            "reference_reachable_pairs": positives, "preserved_reachable_pairs": maintained,
            "matched_endpoint_probes": len(mapped), "reference_endpoint_probes": len(probes)}


def evidence(sample, candidate, reference, folder):
    task = sample["task"]
    result = {"candidate": candidate.stats()}
    bounds = extent(candidate.lines)
    if task == "synthetic":
        result["directed_roads_first_80"] = candidate.road_rows()
    else:
        aligned = task == "edit" or real_alignment(reference, candidate)
        result["reference"] = reference.stats()
        result["shared_viewport"] = aligned
        rb = extent(reference.lines)
        render(reference.lines, rb).save(folder / "reference.png")
        if aligned:
            bounds = rb
        if task == "edit":
            result["changes_from_input"] = edit_diff(reference, candidate, sample["expected"]["target_edge"])
        elif aligned:
            rc = grid_coverage(reference.lines)
            pc = grid_coverage(candidate.lines)
            common = len(rc & pc)
            result["occupied_5m_cells"] = {"reference": len(rc), "output": len(pc), "overlap": common}
            result["reachability"] = route_probes(reference, candidate)
            result["reference_bounds_m"], result["candidate_bounds_m"] = rb, extent(candidate.lines)
        else:
            result["alignment_note"] = "Cannot establish candidate georeferencing. Panels independently scaled; visual resemblance alone cannot establish the requested location."
    render(candidate.lines, bounds).save(folder / "candidate.png")
    return result
