import React from "react";

// A Markdown-subset renderer that emits React elements only. It never uses
// dangerouslySetInnerHTML, so no HTML string — server-produced or otherwise —
// is ever injected into the DOM by the client. Text is escaped by React
// construction. This is the app's single rendering path for node bodies and
// compiled projections; it is strictly stronger than the "server-produced
// sanitised HTML" invariant because the client injects no HTML at all.

type Inline = React.ReactNode;

const SAFE_SCHEME = /^(https?:|mailto:|#|\/)/i;

function safeHref(href: string): string | undefined {
  const trimmed = href.trim();
  if (SAFE_SCHEME.test(trimmed)) return trimmed;
  return undefined;
}

// Inline parsing: code spans, bold, italic, links. Order matters.
function renderInline(text: string, keyBase: string): Inline[] {
  const out: Inline[] = [];
  let rest = text;
  let i = 0;
  const push = (node: Inline) => out.push(node);

  // Tokenise by scanning for the earliest special marker.
  const patterns: { re: RegExp; kind: string }[] = [
    { re: /`([^`]+)`/, kind: "code" },
    { re: /\*\*([^*]+)\*\*/, kind: "strong" },
    { re: /(?<!\*)\*(?!\*)([^*]+)\*(?!\*)/, kind: "em" },
    { re: /\[([^\]]+)\]\(([^)]+)\)/, kind: "link" },
  ];

  while (rest.length > 0) {
    let earliest: { index: number; match: RegExpMatchArray; kind: string } | null = null;
    for (const p of patterns) {
      const m = rest.match(p.re);
      if (m && m.index !== undefined) {
        if (!earliest || m.index < earliest.index) {
          earliest = { index: m.index, match: m, kind: p.kind };
        }
      }
    }
    if (!earliest) {
      push(rest);
      break;
    }
    if (earliest.index > 0) push(rest.slice(0, earliest.index));
    const m = earliest.match;
    const k = `${keyBase}-${i++}`;
    if (earliest.kind === "code") {
      push(<code key={k}>{m[1]}</code>);
    } else if (earliest.kind === "strong") {
      push(<strong key={k}>{renderInline(m[1]!, k)}</strong>);
    } else if (earliest.kind === "em") {
      push(<em key={k}>{renderInline(m[1]!, k)}</em>);
    } else if (earliest.kind === "link") {
      const href = safeHref(m[2]!);
      if (href) {
        push(
          <a key={k} href={href} rel="noreferrer noopener" target="_blank">
            {renderInline(m[1]!, k)}
          </a>,
        );
      } else {
        push(m[1]!);
      }
    }
    rest = rest.slice(earliest.index + m[0].length);
  }
  return out;
}

interface Block {
  render: (key: string) => React.ReactNode;
}

/** Parse a Markdown-subset string into block-level React nodes. */
export function renderMarkdown(src: string): React.ReactNode[] {
  const lines = src.replace(/\r\n?/g, "\n").split("\n");
  const blocks: Block[] = [];
  let idx = 0;

  while (idx < lines.length) {
    const line = lines[idx]!;

    // Fenced code (``` or ```mermaid)
    const fence = line.match(/^```(\w*)\s*$/);
    if (fence) {
      const lang = fence[1] ?? "";
      const body: string[] = [];
      idx++;
      while (idx < lines.length && !/^```\s*$/.test(lines[idx]!)) {
        body.push(lines[idx]!);
        idx++;
      }
      idx++; // consume closing fence
      const code = body.join("\n");
      if (lang === "mermaid") {
        blocks.push({
          render: (k) => (
            <figure key={k} className="md-mermaid" aria-label="Mermaid diagram source">
              <figcaption className="md-mermaid-cap">Diagram (Mermaid)</figcaption>
              <pre className="md-pre">
                <code>{code}</code>
              </pre>
            </figure>
          ),
        });
      } else {
        blocks.push({
          render: (k) => (
            <pre key={k} className="md-pre">
              <code>{code}</code>
            </pre>
          ),
        });
      }
      continue;
    }

    // Blank line
    if (/^\s*$/.test(line)) {
      idx++;
      continue;
    }

    // Heading
    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if (h) {
      const level = h[1]!.length;
      const content = h[2]!;
      const Tag = (`h${Math.min(level, 6)}`) as keyof React.JSX.IntrinsicElements;
      blocks.push({
        render: (k) => <Tag key={k}>{renderInline(content, k)}</Tag>,
      });
      idx++;
      continue;
    }

    // Horizontal rule
    if (/^(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
      blocks.push({ render: (k) => <hr key={k} /> });
      idx++;
      continue;
    }

    // Blockquote
    if (/^>\s?/.test(line)) {
      const quote: string[] = [];
      while (idx < lines.length && /^>\s?/.test(lines[idx]!)) {
        quote.push(lines[idx]!.replace(/^>\s?/, ""));
        idx++;
      }
      blocks.push({
        render: (k) => (
          <blockquote key={k}>
            {renderMarkdown(quote.join("\n"))}
          </blockquote>
        ),
      });
      continue;
    }

    // Unordered list
    if (/^\s*[-*+]\s+/.test(line)) {
      const items: string[] = [];
      while (idx < lines.length && /^\s*[-*+]\s+/.test(lines[idx]!)) {
        items.push(lines[idx]!.replace(/^\s*[-*+]\s+/, ""));
        idx++;
      }
      blocks.push({
        render: (k) => (
          <ul key={k}>
            {items.map((it, j) => (
              <li key={`${k}-${j}`}>{renderInline(it, `${k}-${j}`)}</li>
            ))}
          </ul>
        ),
      });
      continue;
    }

    // Ordered list
    if (/^\s*\d+[.)]\s+/.test(line)) {
      const items: string[] = [];
      while (idx < lines.length && /^\s*\d+[.)]\s+/.test(lines[idx]!)) {
        items.push(lines[idx]!.replace(/^\s*\d+[.)]\s+/, ""));
        idx++;
      }
      blocks.push({
        render: (k) => (
          <ol key={k}>
            {items.map((it, j) => (
              <li key={`${k}-${j}`}>{renderInline(it, `${k}-${j}`)}</li>
            ))}
          </ol>
        ),
      });
      continue;
    }

    // Paragraph (gather until blank / block start)
    const para: string[] = [];
    while (
      idx < lines.length &&
      !/^\s*$/.test(lines[idx]!) &&
      !/^```/.test(lines[idx]!) &&
      !/^(#{1,6})\s+/.test(lines[idx]!) &&
      !/^>\s?/.test(lines[idx]!) &&
      !/^\s*[-*+]\s+/.test(lines[idx]!) &&
      !/^\s*\d+[.)]\s+/.test(lines[idx]!)
    ) {
      para.push(lines[idx]!);
      idx++;
    }
    const text = para.join(" ");
    blocks.push({ render: (k) => <p key={k}>{renderInline(text, k)}</p> });
  }

  return blocks.map((b, j) => b.render(`b${j}`));
}

/** A component wrapper. */
export function Markdown({ text, className }: { text: string; className?: string }) {
  return <div className={className ?? "md"}>{renderMarkdown(text)}</div>;
}
