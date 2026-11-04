import type { Metadata } from "next";
import { Shell } from "@/components/Shell";
import "./globals.css";

export const metadata: Metadata = { title: "YouTube AI Agent Pro", description: "Autonomous YouTube channel manager" };

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" data-theme="light">
      <head>
      </head>
      <body><Shell>{children}</Shell></body>
    </html>
  );
}
