import { type SVGProps } from "react";

// Grok (xAI) mark — the slashed diagonal glyph, currentColor so it inherits the
// surrounding text color like the other harness icons.
export function GrokIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true" {...props}>
      <path
        d="M6.6 18 15.2 6.4M11.4 18l5.9-8"
        stroke="currentColor"
        strokeWidth="2.1"
        strokeLinecap="round"
      />
    </svg>
  );
}
