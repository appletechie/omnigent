import { type SVGProps } from "react";

// Droid (Factory) mark — a robot head, currentColor so it inherits text color.
export function DroidIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true" {...props}>
      <path d="M12 2.4a1 1 0 0 1 1 1V5h1.5A3.5 3.5 0 0 1 18 8.5v6A3.5 3.5 0 0 1 14.5 18h-5A3.5 3.5 0 0 1 6 14.5v-6A3.5 3.5 0 0 1 9.5 5H11V3.4a1 1 0 0 1 1-1Zm-2.1 7.9a1.3 1.3 0 1 0 0 2.6 1.3 1.3 0 0 0 0-2.6Zm4.2 0a1.3 1.3 0 1 0 0 2.6 1.3 1.3 0 0 0 0-2.6Z" />
      <path d="M4.5 9.4a1 1 0 0 1 1 1v3a1 1 0 1 1-2 0v-3a1 1 0 0 1 1-1Zm15 0a1 1 0 0 1 1 1v3a1 1 0 1 1-2 0v-3a1 1 0 0 1 1-1Z" />
    </svg>
  );
}
