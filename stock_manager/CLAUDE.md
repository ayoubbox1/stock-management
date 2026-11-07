# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

This is a premium SaaS dashboard built with Next.js, TypeScript, and Tailwind CSS.

## Project Setup
- Build: `npm run build` (Next.js production build)
- Dev server: `npm run dev`
- Lint: `npm run lint`

## Project Architecture
```
src/
  app/           - Next.js App Router pages
    page.tsx     - Main dashboard
    analytics/   - Analytics page
    users/       - Users management page
    billing/     - Billing & subscription page
    settings/    - Account settings page
  components/    - Reusable UI components
    sidebar.tsx  - Navigation sidebar (responsive)
    stat-card.tsx - KPI stat cards
    sales-chart.tsx - Recharts bar chart
    data-table.tsx - Sortable/searchable data table
    contact-form.tsx - Form validation (react-hook-form + zod)
    ui/          - shadcn/ui components (Button, Toggle)
  lib/
    utils.ts     - Shared utilities (cn, formatters)
```

## Key Frameworks & Libraries
- Next.js 15 (App Router)
- TypeScript 5
- Tailwind CSS 3 + shadcn/ui design system
- React Hook Form + Zod (form validation)
- Recharts (data visualization)
- Lucide React (icons)
- Jest + Testing Library (automated tests)

## Design System
- Color tokens: primary (blue), success (green), destructive (red), warning (amber)
- Responsive layout: sidebar collapses on mobile, shows overlay
- Dark/light mode toggle with `next-themes`
- Focus-visible ring accessibility patterns

## Testing
- Component tests with @testing-library/react
- Coverage threshold: 80% (branches, functions, lines, statements)
- Key tests: Button, Toggle, StatCard, ContactForm, DataTable, Sidebar
