import type { ControlStatus, Grade } from "@/lib/types"

const money = new Intl.NumberFormat("en-IE", {
  style: "currency",
  currency: "EUR",
})

export function formatMoney(cents: number) {
  return money.format(cents / 100)
}

export function gradeClass(grade: Grade) {
  switch (grade) {
    case "A":
      return "border-green-300 bg-green-100 text-green-900"
    case "B":
      return "border-lime-300 bg-lime-100 text-lime-900"
    case "C":
      return "border-amber-300 bg-amber-100 text-amber-900"
    case "D":
      return "border-orange-300 bg-orange-100 text-orange-900"
    case "E":
      return "border-red-300 bg-red-100 text-red-900"
    default:
      return "border-slate-300 bg-slate-100 text-slate-600"
  }
}

export function controlClass(status: ControlStatus) {
  switch (status) {
    case "verified":
      return "border-green-300 bg-green-50 text-green-800"
    case "violated":
      return "border-red-300 bg-red-50 text-red-800"
    case "unobserved":
      return "border-slate-300 bg-slate-50 text-slate-700"
  }
}
