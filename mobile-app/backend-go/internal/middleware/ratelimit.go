package middleware

import (
	"sync"
	"time"

	"github.com/gofiber/fiber/v2"
)

type visitor struct {
	count    int
	windowAt time.Time
}

// RateLimiter is a lightweight in-memory limiter keyed by API key + IP.
func RateLimiter(max int, window time.Duration) fiber.Handler {
	if max <= 0 {
		max = 120
	}
	if window <= 0 {
		window = time.Minute
	}

	var mu sync.Mutex
	visitors := map[string]*visitor{}

	go func() {
		t := time.NewTicker(window)
		defer t.Stop()
		for range t.C {
			mu.Lock()
			now := time.Now()
			for k, v := range visitors {
				if now.Sub(v.windowAt) > window*2 {
					delete(visitors, k)
				}
			}
			mu.Unlock()
		}
	}()

	return func(c *fiber.Ctx) error {
		key := c.Get("X-API-Key")
		if key == "" {
			key = c.IP()
		} else {
			key = key + "|" + c.IP()
		}

		now := time.Now()
		mu.Lock()
		v, ok := visitors[key]
		if !ok || now.Sub(v.windowAt) >= window {
			visitors[key] = &visitor{count: 1, windowAt: now}
			mu.Unlock()
			return c.Next()
		}
		if v.count >= max {
			mu.Unlock()
			return c.Status(fiber.StatusTooManyRequests).JSON(fiber.Map{
				"error": "rate limit exceeded",
			})
		}
		v.count++
		mu.Unlock()
		return c.Next()
	}
}
