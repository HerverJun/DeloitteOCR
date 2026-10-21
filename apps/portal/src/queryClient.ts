import { QueryClient } from '@tanstack/react-query'

// Shared cache without importing the route tree from session/authentication code.
export const queryClient = new QueryClient({ defaultOptions: {
  queries: { retry: false, staleTime: 15_000, refetchOnWindowFocus: false },
} })
