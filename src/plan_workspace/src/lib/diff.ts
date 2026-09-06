// A small line + word diff for the compiled-Markdown side-by-side view. Classic
// LCS over lines, then a token diff within changed lines for word-level marks.
// Deterministic and dependency-free.

export type LineOp = { type: "same" | "add" | "del"; text: string };

export function diffLines(a: string, b: string): LineOp[] {
  const al = a.split("\n");
  const bl = b.split("\n");
  const n = al.length;
  const m = bl.length;
  const lcs: number[][] = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      lcs[i]![j] = al[i] === bl[j] ? lcs[i + 1]![j + 1]! + 1 : Math.max(lcs[i + 1]![j]!, lcs[i]![j + 1]!);
    }
  }
  const out: LineOp[] = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (al[i] === bl[j]) {
      out.push({ type: "same", text: al[i]! });
      i++;
      j++;
    } else if (lcs[i + 1]![j]! >= lcs[i]![j + 1]!) {
      out.push({ type: "del", text: al[i]! });
      i++;
    } else {
      out.push({ type: "add", text: bl[j]! });
      j++;
    }
  }
  while (i < n) out.push({ type: "del", text: al[i++]! });
  while (j < m) out.push({ type: "add", text: bl[j++]! });
  return out;
}

export type WordOp = { type: "same" | "add" | "del"; text: string };

export function diffWords(a: string, b: string): { left: WordOp[]; right: WordOp[] } {
  const at = a.split(/(\s+)/);
  const bt = b.split(/(\s+)/);
  const n = at.length;
  const m = bt.length;
  const lcs: number[][] = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      lcs[i]![j] = at[i] === bt[j] ? lcs[i + 1]![j + 1]! + 1 : Math.max(lcs[i + 1]![j]!, lcs[i]![j + 1]!);
    }
  }
  const left: WordOp[] = [];
  const right: WordOp[] = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (at[i] === bt[j]) {
      left.push({ type: "same", text: at[i]! });
      right.push({ type: "same", text: bt[j]! });
      i++;
      j++;
    } else if (lcs[i + 1]![j]! >= lcs[i]![j + 1]!) {
      left.push({ type: "del", text: at[i]! });
      i++;
    } else {
      right.push({ type: "add", text: bt[j]! });
      j++;
    }
  }
  while (i < n) left.push({ type: "del", text: at[i++]! });
  while (j < m) right.push({ type: "add", text: bt[j++]! });
  return { left, right };
}
