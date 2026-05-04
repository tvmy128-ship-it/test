import { WifiOff } from "lucide-react";
import { ButtonLink } from "@/components/ui/button";

export default function OfflinePage() {
  return (
    <main className="flex min-h-screen items-center justify-center bg-cream px-4">
      <section className="max-w-md rounded-[2rem] bg-white p-6 text-center shadow-xl shadow-neutral-900/10">
        <div className="mx-auto flex h-14 w-14 items-center justify-center rounded-3xl bg-maple text-white">
          <WifiOff className="h-7 w-7" />
        </div>
        <h1 className="mt-4 text-3xl font-black">You are offline</h1>
        <p className="mt-2 text-sm leading-6 text-muted">DealNear needs a connection to refresh nearby offers, approvals, and live deal availability.</p>
        <ButtonLink href="/" className="mt-5">
          Try again
        </ButtonLink>
      </section>
    </main>
  );
}

