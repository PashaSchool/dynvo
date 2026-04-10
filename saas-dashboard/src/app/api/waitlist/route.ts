import { NextRequest, NextResponse } from "next/server";
import fs from "node:fs";
import path from "node:path";

const WAITLIST_FILE = path.join(process.cwd(), "waitlist.json");

interface WaitlistEntry {
  email: string;
  repo_url: string | null;
  timestamp: string;
  status: "pending" | "scanning" | "sent" | "failed";
}

function readWaitlist(): WaitlistEntry[] {
  try {
    if (fs.existsSync(WAITLIST_FILE)) {
      return JSON.parse(fs.readFileSync(WAITLIST_FILE, "utf-8"));
    }
  } catch {
    // corrupted file — start fresh
  }
  return [];
}

function writeWaitlist(entries: WaitlistEntry[]) {
  fs.writeFileSync(WAITLIST_FILE, JSON.stringify(entries, null, 2));
}

export async function POST(request: NextRequest) {
  try {
    const contentType = request.headers.get("content-type") || "";
    let email: string | null = null;
    let repoUrl: string | null = null;

    if (contentType.includes("application/json")) {
      const body = await request.json();
      email = body.email;
      repoUrl = body.repo_url || null;
    } else {
      const formData = await request.formData();
      email = formData.get("email") as string;
      repoUrl = (formData.get("repo_url") as string) || null;
    }

    if (!email || !email.includes("@")) {
      return NextResponse.json(
        { error: "Valid email is required" },
        { status: 400 },
      );
    }

    // Basic GitHub URL validation
    if (
      repoUrl &&
      !repoUrl.match(/^https?:\/\/(www\.)?github\.com\/.+\/.+/)
    ) {
      return NextResponse.json(
        {
          error:
            "Please provide a valid GitHub repo URL (https://github.com/org/repo)",
        },
        { status: 400 },
      );
    }

    const entries = readWaitlist();

    // Check duplicate (same email + same repo)
    const isDuplicate = entries.some(
      (e) =>
        e.email.toLowerCase() === email!.toLowerCase() &&
        (e.repo_url || "") === (repoUrl || ""),
    );

    if (isDuplicate) {
      return NextResponse.redirect(new URL("/?joined=1", request.url), 303);
    }

    entries.push({
      email: email.toLowerCase(),
      repo_url: repoUrl,
      timestamp: new Date().toISOString(),
      status: "pending",
    });
    writeWaitlist(entries);

    const repoLabel = repoUrl ? ` (${repoUrl})` : "";
    console.log(`[waitlist] +${email}${repoLabel} (total: ${entries.length})`);

    return NextResponse.redirect(new URL("/?joined=1", request.url), 303);
  } catch (e) {
    return NextResponse.json(
      { error: "Something went wrong" },
      { status: 500 },
    );
  }
}

export async function GET() {
  const entries = readWaitlist();
  const pending = entries.filter((e) => e.status === "pending").length;
  return NextResponse.json({
    total: entries.length,
    pending,
    with_repo: entries.filter((e) => e.repo_url).length,
  });
}
