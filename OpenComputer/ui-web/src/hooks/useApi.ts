import { useEffect, useState } from "react";
import { api, ApiError } from "@/lib/api";

export interface UseApiState<T> {
  data: T | undefined;
  error: ApiError | undefined;
  loading: boolean;
  refetch: () => void;
}

export function useApi<T>(path: string, deps: unknown[] = []): UseApiState<T> {
  const [data, setData] = useState<T | undefined>();
  const [error, setError] = useState<ApiError | undefined>();
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(undefined);
    api<T>(path)
      .then((d) => {
        if (!cancelled) {
          setData(d);
          setLoading(false);
        }
      })
      .catch((e: ApiError) => {
        if (!cancelled) {
          setError(e);
          setLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, tick, ...deps]);

  return { data, error, loading, refetch: () => setTick((n) => n + 1) };
}
