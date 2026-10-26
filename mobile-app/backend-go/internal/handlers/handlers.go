package handlers

import (
	"context"
	"log"
	"strings"
	"time"

	"github.com/gofiber/contrib/websocket"
	"github.com/gofiber/fiber/v2"
	"github.com/mohammadgholamisama/v2ray-ping-hub/internal/models"
	"github.com/mohammadgholamisama/v2ray-ping-hub/internal/services"
	"github.com/mohammadgholamisama/v2ray-ping-hub/internal/ws"
)

type API struct {
	Agg   *services.Aggregator
	Panel *services.PanelSyncService
	Hub   *ws.Hub
}

func (a *API) Health(c *fiber.Ctx) error {
	return c.JSON(models.HealthResponse{
		OK:      true,
		Service: "v2ray-ping-hub",
		Time:    time.Now().Unix(),
	})
}

func (a *API) PostPings(c *fiber.Ctx) error {
	var req models.PingBatchRequest
	if err := c.BodyParser(&req); err != nil {
		return c.Status(fiber.StatusBadRequest).JSON(fiber.Map{"error": "invalid json body"})
	}
	req.Operator = models.Operator(strings.ToLower(string(req.Operator)))
	if !req.Operator.Valid() {
		return c.Status(fiber.StatusBadRequest).JSON(fiber.Map{
			"error": "operator must be irancell | mci | rightel",
		})
	}
	if len(req.Samples) == 0 {
		return c.Status(fiber.StatusBadRequest).JSON(fiber.Map{"error": "samples required"})
	}
	if len(req.Samples) > 500 {
		return c.Status(fiber.StatusBadRequest).JSON(fiber.Map{"error": "too many samples (max 500)"})
	}

	for i := range req.Samples {
		req.Samples[i].ConfigHash = strings.TrimSpace(strings.ToLower(req.Samples[i].ConfigHash))
		if req.Samples[i].Operator == "" {
			req.Samples[i].Operator = req.Operator
		} else {
			req.Samples[i].Operator = models.Operator(strings.ToLower(string(req.Samples[i].Operator)))
		}
	}

	n, err := a.Agg.IngestBatch(c.Context(), req)
	if err != nil {
		return c.Status(fiber.StatusBadRequest).JSON(fiber.Map{"error": err.Error()})
	}

	list, err := a.Agg.Ranked(c.Context())
	if err != nil {
		return c.Status(fiber.StatusInternalServerError).JSON(fiber.Map{"error": err.Error()})
	}

	a.Hub.BroadcastRanked(list)

	// Panel reorder runs on the PingTTL scheduler (default 15m) — not on every live upload.
	return c.JSON(fiber.Map{
		"ok":       true,
		"accepted": n,
		"ranked":   list,
	})
}

func (a *API) GetRanked(c *fiber.Ctx) error {
	list, err := a.Agg.Ranked(c.Context())
	if err != nil {
		return c.Status(fiber.StatusInternalServerError).JSON(fiber.Map{"error": err.Error()})
	}
	return c.JSON(list)
}

type panelSyncBody struct {
	// Optional: push exact page averages (host/port/match_host) from the app.
	// When empty, falls back to multi-device 15m Redis window.
	Items []models.FingerprintRank `json:"items"`
}

func (a *API) SyncPanelNow(c *fiber.Ctx) error {
	var body panelSyncBody
	_ = c.BodyParser(&body)

	ranks := body.Items
	source := "client"
	if len(ranks) == 0 {
		source = "window"
		var err error
		ranks, err = a.Agg.RankedFingerprints(c.Context())
		if err != nil {
			return c.Status(fiber.StatusInternalServerError).JSON(fiber.Map{"error": err.Error()})
		}
	}

	// Keep only successful averages — untested/failed never reorder the panel
	filtered := make([]models.FingerprintRank, 0, len(ranks))
	for _, r := range ranks {
		if r.AvgPing > 0 && strings.TrimSpace(r.Host) != "" && r.Port > 0 {
			filtered = append(filtered, r)
		}
	}
	if len(filtered) == 0 {
		return c.JSON(fiber.Map{
			"ok":      true,
			"count":   0,
			"source":  source,
			"message": "no successful ping averages to apply",
		})
	}

	if err := a.Panel.SyncPingRanks(c.Context(), filtered); err != nil {
		return c.Status(fiber.StatusBadGateway).JSON(fiber.Map{"error": err.Error()})
	}
	return c.JSON(fiber.Map{
		"ok":     true,
		"count":  len(filtered),
		"source": source,
	})
}

// RunPanelSyncLoop aggregates the last window and reorders Astra every interval.
func (a *API) RunPanelSyncLoop(interval time.Duration) {
	if interval < time.Minute {
		interval = 15 * time.Minute
	}
	log.Printf("[panel-sync] scheduler every %s (first sync after one window)", interval)
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for range ticker.C {
		a.syncOnce()
	}
}

func (a *API) syncOnce() {
	ctx, cancel := context.WithTimeout(context.Background(), 45*time.Second)
	defer cancel()
	ranks, err := a.Agg.RankedFingerprints(ctx)
	if err != nil {
		log.Printf("[panel-sync] rank: %v", err)
		return
	}
	if len(ranks) == 0 {
		log.Printf("[panel-sync] skip — no fingerprints in window")
		return
	}
	if err := a.Panel.SyncPingRanks(ctx, ranks); err != nil {
		log.Printf("[panel-sync] %v", err)
		return
	}
	log.Printf("[panel-sync] ok — reordered %d fingerprints in Astra", len(ranks))
}

// WSUpgrade gates non-websocket requests.
func WSUpgrade(c *fiber.Ctx) error {
	if websocket.IsWebSocketUpgrade(c) {
		return c.Next()
	}
	return fiber.ErrUpgradeRequired
}

func (a *API) WSRanked(c *websocket.Conn) {
	a.Hub.Register(c)
	defer a.Hub.Unregister(c)

	list, err := a.Agg.Ranked(context.Background())
	if err == nil {
		payload, _ := jsonMarshalWS(list)
		_ = c.WriteMessage(websocket.TextMessage, payload)
	}

	for {
		if _, _, err := c.ReadMessage(); err != nil {
			break
		}
	}
}

func jsonMarshalWS(list models.RankedListResponse) ([]byte, error) {
	msg := models.WSMessage{Type: "ranked", Payload: list}
	return jsonMarshal(msg)
}
