package config

import (
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"
)

type Config struct {
	Port                   string
	BindHost               string
	APIKey                 string
	RedisAddr              string
	RedisPassword          string
	RedisDB                int
	PingTTL                time.Duration
	RateLimitMax           int
	RateLimitWindow        time.Duration
	PanelSyncEnabled       bool
	PanelBaseURL           string
	PanelAPIPath           string
	PanelAPIKey            string
	PanelTimeout           time.Duration
}

func Load() (*Config, error) {
	cfg := &Config{
		Port:            getenv("PORT", "8080"),
		BindHost:        getenv("BIND_HOST", "127.0.0.1"),
		APIKey:          strings.TrimSpace(os.Getenv("API_KEY")),
		RedisAddr:       getenv("REDIS_ADDR", "127.0.0.1:6379"),
		RedisPassword:   os.Getenv("REDIS_PASSWORD"),
		RedisDB:         getenvInt("REDIS_DB", 0),
		PingTTL:         time.Duration(getenvInt("PING_TTL_SECONDS", 900)) * time.Second,
		RateLimitMax:    getenvInt("RATE_LIMIT_MAX", 120),
		RateLimitWindow: time.Duration(getenvInt("RATE_LIMIT_WINDOW_SECONDS", 60)) * time.Second,
		PanelSyncEnabled: getenvBool("PANEL_SYNC_ENABLED", true),
		PanelBaseURL:    strings.TrimRight(getenv("PANEL_BASE_URL", "http://127.0.0.1"), "/"),
		PanelAPIPath:    getenv("PANEL_API_PATH", "/api/admin/config-order"),
		PanelAPIKey:     strings.TrimSpace(os.Getenv("PANEL_API_KEY")),
		PanelTimeout:    time.Duration(getenvInt("PANEL_TIMEOUT_SECONDS", 15)) * time.Second,
	}

	if cfg.APIKey == "" {
		return nil, fmt.Errorf("API_KEY is required")
	}
	if cfg.PingTTL < time.Minute {
		cfg.PingTTL = 15 * time.Minute
	}
	return cfg, nil
}

func (c *Config) PanelSyncURL() string {
	path := c.PanelAPIPath
	if !strings.HasPrefix(path, "/") {
		path = "/" + path
	}
	return c.PanelBaseURL + path
}

func getenv(key, fallback string) string {
	if v := strings.TrimSpace(os.Getenv(key)); v != "" {
		return v
	}
	return fallback
}

func getenvInt(key string, fallback int) int {
	v := strings.TrimSpace(os.Getenv(key))
	if v == "" {
		return fallback
	}
	n, err := strconv.Atoi(v)
	if err != nil {
		return fallback
	}
	return n
}

func getenvBool(key string, fallback bool) bool {
	v := strings.TrimSpace(strings.ToLower(os.Getenv(key)))
	if v == "" {
		return fallback
	}
	switch v {
	case "1", "true", "yes", "on":
		return true
	case "0", "false", "no", "off":
		return false
	default:
		return fallback
	}
}
