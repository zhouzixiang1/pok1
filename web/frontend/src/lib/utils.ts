import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export function compactBotName(name: string) {
  return name.replace(/^national_/, "").replace(/^claude_/, "");
}
