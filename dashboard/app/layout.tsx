import type { Metadata } from "next";

import { Nav } from "@/components/nav";
import { Providers } from "@/components/providers";

import "./globals.css";

export const metadata: Metadata = {
  title: "IdeaI",
  description: "Catalogo di idee prodotto estratte dalle fonti di segnale",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="it">
      <body>
        <Providers>
          <Nav />
          <main className="mx-auto max-w-7xl px-4 py-6">{children}</main>
        </Providers>
      </body>
    </html>
  );
}
