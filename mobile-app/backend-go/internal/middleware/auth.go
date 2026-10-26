package middleware

import (
	"crypto/subtle"
	"strings"

	"github.com/gofiber/fiber/v2"
)

// APIKey protects routes with X-API-Key (also accepts Authorization: ApiKey …).
func APIKey(expected string) fiber.Handler {
	want := []byte(strings.TrimSpace(expected))
	return func(c *fiber.Ctx) error {
		got := strings.TrimSpace(c.Get("X-API-Key"))
		if got == "" {
			got = strings.TrimSpace(c.Get("X-Api-Key"))
		}
		if got == "" {
			auth := c.Get("Authorization")
			const prefix = "ApiKey "
			if len(auth) >= len(prefix) && strings.EqualFold(auth[:len(prefix)], prefix) {
				got = strings.TrimSpace(auth[len(prefix):])
			}
		}
		if len(want) == 0 || subtle.ConstantTimeCompare([]byte(got), want) != 1 {
			return c.Status(fiber.StatusUnauthorized).JSON(fiber.Map{
				"error": "unauthorized",
			})
		}
		return c.Next()
	}
}
