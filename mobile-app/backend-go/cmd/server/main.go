package main

import (
	"context"
	"crypto/subtle"
	"log"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/gofiber/contrib/websocket"
	"github.com/gofiber/fiber/v2"
	"github.com/gofiber/fiber/v2/middleware/cors"
	"github.com/gofiber/fiber/v2/middleware/logger"
	"github.com/gofiber/fiber/v2/middleware/recover"
	"github.com/joho/godotenv"
	"github.com/redis/go-redis/v9"

	"github.com/mohammadgholamisama/v2ray-ping-hub/internal/config"
	"github.com/mohammadgholamisama/v2ray-ping-hub/internal/handlers"
	"github.com/mohammadgholamisama/v2ray-ping-hub/internal/middleware"
	"github.com/mohammadgholamisama/v2ray-ping-hub/internal/services"
	"github.com/mohammadgholamisama/v2ray-ping-hub/internal/ws"
)

func subtleConstantTimeEq(got, want string) bool {
	a := []byte(strings.TrimSpace(got))
	b := []byte(strings.TrimSpace(want))
	if len(b) == 0 {
		return false
	}
	return subtle.ConstantTimeCompare(a, b) == 1
}

func main() {
	_ = godotenv.Load()

	cfg, err := config.Load()
	if err != nil {
		log.Fatalf("config: %v", err)
	}

	rdb := redis.NewClient(&redis.Options{
		Addr:     cfg.RedisAddr,
		Password: cfg.RedisPassword,
		DB:       cfg.RedisDB,
	})
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := rdb.Ping(ctx).Err(); err != nil {
		log.Fatalf("redis: %v", err)
	}

	agg := services.NewAggregator(rdb, cfg.PingTTL)
	panel := services.NewPanelSyncService(cfg)
	hub := ws.NewHub()
	api := &handlers.API{Agg: agg, Panel: panel, Hub: hub}

	app := fiber.New(fiber.Config{
		AppName:      "V2Ray Ping Hub",
		ReadTimeout:  15 * time.Second,
		WriteTimeout: 30 * time.Second,
		IdleTimeout:  60 * time.Second,
		BodyLimit:    2 * 1024 * 1024,
	})

	app.Use(recover.New())
	app.Use(logger.New())
	app.Use(cors.New(cors.Config{
		AllowOrigins: "*",
		AllowHeaders: "Origin, Content-Type, Accept, Authorization, X-API-Key, X-Api-Key",
		AllowMethods: "GET,POST,PUT,OPTIONS",
	}))

	app.Get("/health", api.Health)

	v1 := app.Group("/api/v1",
		middleware.APIKey(cfg.APIKey),
		middleware.RateLimiter(cfg.RateLimitMax, cfg.RateLimitWindow),
	)
	v1.Post("/pings", api.PostPings)
	v1.Get("/ranked", api.GetRanked)
	v1.Post("/panel/sync", api.SyncPanelNow)

	// Every PingTTL (default 10m): average multi-device samples → reorder Astra config-order.
	go api.RunPanelSyncLoop(cfg.PingTTL)

	app.Use("/ws/ranked", func(c *fiber.Ctx) error {
		key := c.Query("api_key")
		if key == "" {
			key = c.Get("X-API-Key")
		}
		if subtleConstantTimeEq(key, cfg.APIKey) {
			return c.Next()
		}
		return fiber.ErrUnauthorized
	}, handlers.WSUpgrade)
	app.Get("/ws/ranked", websocket.New(api.WSRanked, websocket.Config{
		ReadBufferSize:  1024,
		WriteBufferSize: 1024,
	}))

	go func() {
		host := strings.TrimSpace(cfg.BindHost)
		if host == "" {
			host = "127.0.0.1"
		}
		addr := host + ":" + cfg.Port
		log.Printf("v2ray-ping-hub listening on %s (redis=%s panel=%s)", addr, cfg.RedisAddr, cfg.PanelSyncURL())
		if err := app.Listen(addr); err != nil {
			log.Fatalf("listen: %v", err)
		}
	}()

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)
	<-stop
	log.Println("shutting down…")
	_ = app.Shutdown()
	_ = rdb.Close()
}
