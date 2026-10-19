import "./globals.css";
import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "Bidu · Vlog rough cuts",
  description: "Turn a folder of clips into a rough cut you can edit and download.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <div className="shell">
          <header className="topbar">
            <a className="brand" href="/">
              <span className="brand-mark" aria-hidden>▶</span>
              Bidu
            </a>
            <span className="muted small">Rough cuts for vlogs</span>
          </header>
          {children}
        </div>
      </body>
    </html>
  );
}
