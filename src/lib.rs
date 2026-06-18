//! ffroute: a Starlette-compatible route matcher as a native Python extension.
//!
//! Build step ("JIT"): the route patterns are compiled once into a segment
//! trie (radix tree over `/`-split segments). Match step: walk the trie,
//! returning the *minimum registration index* among all matching routes --
//! exactly Starlette's "first-registered match wins" semantics, including its
//! static-vs-param shadowing behaviour.
//!
//! Segment kinds, mirroring Starlette's convertors:
//!   * static -- exact string (HashMap child)
//!   * plain `{name}`/`{name:str}` -- `[^/]+`, a regex-free fast path
//!   * compound / typed -- `{n:int}`, `{n:float}`, `{n:uuid}`, or a
//!     literal+param like `{username}:disable` --
//!     compiled to an anchored whole-segment regex
//!   * `{name:path}` -- `.*`, consumes the remainder of the path

use pyo3::prelude::*;
use regex::Regex;
use std::collections::HashMap;

const NO_MATCH: i32 = i32::MAX;

enum SegKind {
    Static,
    StrParam,
    Path,
    Dyn,
}

fn classify(seg: &str) -> SegKind {
    if !seg.contains('{') {
        return SegKind::Static;
    }
    let inner_ok = |s: &str| {
        !s.is_empty() && (s.as_bytes()[0] == b'_' || s.as_bytes()[0].is_ascii_alphabetic())
    };
    // pure `{name}` or `{name:str}`
    if seg.starts_with('{') && seg.ends_with('}') {
        let inner = &seg[1..seg.len() - 1];
        let (name, suffix) = match inner.split_once(':') {
            Some((n, s)) => (n, Some(s)),
            None => (inner, None),
        };
        if inner_ok(name) {
            match suffix {
                None | Some("str") => return SegKind::StrParam,
                Some("path") => return SegKind::Path,
                _ => {}
            }
        }
    }
    SegKind::Dyn
}

/// Convertor regex bodies (Starlette convertors.py), verbatim.
fn convertor_regex(conv: &str) -> &'static str {
    match conv {
        "str" => "[^/]+",
        "path" => ".*",
        "int" => "[0-9]+",
        "float" => r"[0-9]+(\.[0-9]+)?",
        "uuid" => "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        _ => "[^/]+",
    }
}

/// Build an anchored whole-segment regex for a compound segment.
fn compile_segment(seg: &str) -> Regex {
    let mut body = String::from("^");
    let bytes = seg.as_bytes();
    let mut i = 0;
    let mut literal_start = 0;
    while i < bytes.len() {
        if bytes[i] == b'{' {
            if let Some(close) = seg[i..].find('}') {
                let close = i + close;
                body.push_str(&regex::escape(&seg[literal_start..i]));
                let inner = &seg[i + 1..close];
                let conv = inner.split(':').nth(1).unwrap_or("str");
                body.push_str(convertor_regex(conv));
                i = close + 1;
                literal_start = i;
                continue;
            }
        }
        i += 1;
    }
    body.push_str(&regex::escape(&seg[literal_start..]));
    body.push('$');
    // Falls back to a never-matching regex if assembly somehow produces an
    // invalid pattern. Safer than panicking across the Python FFI boundary
    // (release builds use panic=abort, which would terminate the interpreter).
    Regex::new(&body).unwrap_or_else(|_| Regex::new(r"$.^").unwrap())
}

struct Node {
    static_children: HashMap<String, Node>,
    param_child: Option<Box<Node>>, // plain `{str}` fast path
    dyn_children: Vec<(String, Regex, Node)>, // (raw segment, regex, child)
    path_index: i32,
    index: i32,
    // Multiple routes can share one terminal (same path, different methods).
    // `index`/`path_index` keep the smallest (for single-result `match`); these
    // hold the rest so `match_all` returns every candidate.
    extra_index: Vec<i32>,
    extra_path: Vec<i32>,
}

impl Node {
    fn new() -> Self {
        // NB: path_index/index start at NO_MATCH (not the derived-Default 0!) so
        // an empty node doesn't masquerade as a match at registration index 0.
        Node {
            static_children: HashMap::new(),
            param_child: None,
            dyn_children: Vec::new(),
            path_index: NO_MATCH,
            index: NO_MATCH,
            extra_index: Vec::new(),
            extra_path: Vec::new(),
        }
    }
}

struct Trie {
    root: Node,
}

impl Trie {
    fn build(patterns: &[String]) -> Self {
        let mut root = Node::new();
        for (i, pat) in patterns.iter().enumerate() {
            insert(&mut root, pat, i as i32);
        }
        Trie { root }
    }

    fn match_path(&self, path: &str) -> i32 {
        let p = path.strip_prefix('/').unwrap_or(path);
        let r = walk(&self.root, p);
        if r == NO_MATCH {
            -1
        } else {
            r
        }
    }

    fn match_all(&self, path: &str, out: &mut Vec<i32>) {
        let p = path.strip_prefix('/').unwrap_or(path);
        walk_all(&self.root, p, out);
    }
}

fn insert(root: &mut Node, pattern: &str, index: i32) {
    let p = pattern.strip_prefix('/').unwrap_or(pattern);
    let mut node = root;
    for seg in p.split('/') {
        match classify(seg) {
            SegKind::Path => {
                if node.path_index == NO_MATCH {
                    node.path_index = index;
                } else {
                    node.extra_path.push(index);
                }
                return;
            }
            SegKind::StrParam => {
                if node.param_child.is_none() {
                    node.param_child = Some(Box::new(Node::new()));
                }
                node = node.param_child.as_mut().unwrap();
            }
            SegKind::Dyn => {
                let pos = node.dyn_children.iter().position(|(s, _, _)| s == seg);
                let idx = match pos {
                    Some(p) => p,
                    None => {
                        node.dyn_children.push((
                            seg.to_string(),
                            compile_segment(seg),
                            Node::new(),
                        ));
                        node.dyn_children.len() - 1
                    }
                };
                node = &mut node.dyn_children[idx].2;
            }
            SegKind::Static => {
                node = node
                    .static_children
                    .entry(seg.to_string())
                    .or_insert_with(Node::new);
            }
        }
    }
    if node.index == NO_MATCH {
        node.index = index;
    } else {
        node.extra_index.push(index);
    }
}

fn walk(node: &Node, rest: &str) -> i32 {
    let (seg, tail, last) = match rest.find('/') {
        Some(pos) => (&rest[..pos], &rest[pos + 1..], false),
        None => (rest, "", true),
    };

    let mut best = NO_MATCH;

    if let Some(child) = node.static_children.get(seg) {
        let r = if last {
            terminal(child)
        } else {
            walk(child, tail)
        };
        if r < best {
            best = r;
        }
    }
    if !seg.is_empty() {
        if let Some(child) = node.param_child.as_deref() {
            let r = if last {
                terminal(child)
            } else {
                walk(child, tail)
            };
            if r < best {
                best = r;
            }
        }
    }
    for (_, rx, child) in &node.dyn_children {
        if rx.is_match(seg) {
            let r = if last {
                terminal(child)
            } else {
                walk(child, tail)
            };
            if r < best {
                best = r;
            }
        }
    }
    if node.path_index < best {
        best = node.path_index;
    }
    best
}

/// Collect ALL route indices whose path could match `rest` (a superset).
fn walk_all(node: &Node, rest: &str, out: &mut Vec<i32>) {
    let (seg, tail, last) = match rest.find('/') {
        Some(pos) => (&rest[..pos], &rest[pos + 1..], false),
        None => (rest, "", true),
    };
    if last {
        if let Some(child) = node.static_children.get(seg) {
            terminal_all(child, out);
        }
        if !seg.is_empty() {
            if let Some(child) = node.param_child.as_deref() {
                terminal_all(child, out);
            }
        }
        for (_, rx, child) in &node.dyn_children {
            if rx.is_match(seg) {
                terminal_all(child, out);
            }
        }
    } else {
        if let Some(child) = node.static_children.get(seg) {
            walk_all(child, tail, out);
        }
        if !seg.is_empty() {
            if let Some(child) = node.param_child.as_deref() {
                walk_all(child, tail, out);
            }
        }
        for (_, rx, child) in &node.dyn_children {
            if rx.is_match(seg) {
                walk_all(child, tail, out);
            }
        }
    }
    if node.path_index != NO_MATCH {
        out.push(node.path_index); // `:path` consumes the remainder (incl. empty)
        out.extend_from_slice(&node.extra_path);
    }
}

fn terminal_all(node: &Node, out: &mut Vec<i32>) {
    // NB: deliberately does NOT consider `node.path_index`. `path_index`
    // belongs to a `{x:path}` child that requires the parent's trailing
    // slash boundary to have been crossed; reaching `node` terminally
    // (last segment of the probe == `node`'s segment, no `/` after) means
    // we never crossed that boundary, so the `:path` route doesn't apply.
    // The "consumed-with-trailing-slash" case is handled by the
    // `walk_all`-tail push (where the recursion did cross the slash).
    if node.index != NO_MATCH {
        out.push(node.index);
        out.extend_from_slice(&node.extra_index);
    }
}

fn terminal(node: &Node) -> i32 {
    // See `terminal_all` for why `path_index` is intentionally ignored here.
    node.index
}

#[pyclass]
struct Router {
    trie: Trie,
}

#[pymethods]
impl Router {
    #[new]
    fn new(patterns: Vec<String>) -> Self {
        Router {
            trie: Trie::build(&patterns),
        }
    }

    fn match_(&self, path: &str) -> Option<i32> {
        let r = self.trie.match_path(path);
        if r < 0 {
            None
        } else {
            Some(r)
        }
    }

    fn match_many(&self, paths: Vec<String>) -> Vec<i32> {
        paths.iter().map(|p| self.trie.match_path(p)).collect()
    }

    /// All route indices whose path could match (superset for candidate narrowing).
    fn match_all(&self, path: &str) -> Vec<i32> {
        let mut out = Vec::new();
        self.trie.match_all(path, &mut out);
        out
    }
}

#[pymodule]
fn _core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<Router>()?;
    Ok(())
}
