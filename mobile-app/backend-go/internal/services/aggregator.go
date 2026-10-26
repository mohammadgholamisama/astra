package services

import (
	"context"
	"encoding/json"
	"fmt"
	"math"
	"sort"
	"strings"
	"time"

	"github.com/mohammadgholamisama/v2ray-ping-hub/internal/models"
	"github.com/redis/go-redis/v9"
)

const (
	metaKeyPrefix = "cfg:meta:"
	// Latest sample per device/operator/hash — TTL = window (default 10m)
	devPingKeyPrefix = "cfg:devping:"
	indexSetKey      = "cfg:index"
	fpIndexKey       = "cfg:fpindex"
	activeOpsKey     = "ops:active"
)

// Aggregator stores per-device pings and builds multi-device averages.
type Aggregator struct {
	rdb *redis.Client
	ttl time.Duration
}

func NewAggregator(rdb *redis.Client, ttl time.Duration) *Aggregator {
	return &Aggregator{rdb: rdb, ttl: ttl}
}

type configMeta struct {
	ConfigHash string `json:"config_hash"`
	Remark     string `json:"remark"`
	Protocol   string `json:"protocol"`
	Transport  string `json:"transport"`
	Security   string `json:"security"`
	Host       string `json:"host"`
	Port       int    `json:"port"`
	MatchHost  string `json:"match_host"`
}

type pingRecord struct {
	PingMs    int64  `json:"ping_ms"`
	UpdatedAt int64  `json:"updated_at"`
	DeviceID  string `json:"device_id,omitempty"`
}

func cleanHost(h string) string {
	h = strings.ToLower(strings.TrimSpace(h))
	h = strings.Trim(h, "[]")
	h = strings.TrimRight(h, ".")
	if strings.HasPrefix(h, "www.") {
		h = h[4:]
	}
	return h
}

func fingerprint(host string, port int, match string) string {
	return fmt.Sprintf("%s|%d|%s", cleanHost(host), port, cleanHost(match))
}

// IngestBatch stores the latest ping per device (within TTL window).
func (a *Aggregator) IngestBatch(ctx context.Context, req models.PingBatchRequest) (int, error) {
	if !req.Operator.Valid() {
		return 0, fmt.Errorf("invalid operator: %s", req.Operator)
	}
	if len(req.Samples) == 0 {
		return 0, fmt.Errorf("samples is empty")
	}

	now := time.Now().UnixMilli()
	deviceID := strings.TrimSpace(req.DeviceID)
	if deviceID == "" {
		deviceID = "anonymous"
	}

	pipe := a.rdb.Pipeline()
	accepted := 0

	for i := range req.Samples {
		s := &req.Samples[i]
		hash := strings.TrimSpace(strings.ToLower(s.ConfigHash))
		if hash == "" {
			continue
		}
		op := s.Operator
		if op == "" {
			op = req.Operator
		}
		if !op.Valid() {
			continue
		}
		if s.MeasuredAt <= 0 {
			s.MeasuredAt = now
		}
		dev := strings.TrimSpace(s.DeviceID)
		if dev == "" {
			dev = deviceID
		}

		host := cleanHost(s.Host)
		match := cleanHost(s.MatchHost)
		meta := configMeta{
			ConfigHash: hash,
			Remark:     strings.TrimSpace(s.Remark),
			Protocol:   strings.ToLower(strings.TrimSpace(s.Protocol)),
			Transport:  strings.ToLower(strings.TrimSpace(s.Transport)),
			Security:   strings.ToLower(strings.TrimSpace(s.Security)),
			Host:       host,
			Port:       s.Port,
			MatchHost:  match,
		}
		metaBytes, err := json.Marshal(meta)
		if err != nil {
			continue
		}

		rec := pingRecord{
			PingMs:    s.PingMs,
			UpdatedAt: s.MeasuredAt,
			DeviceID:  dev,
		}
		recBytes, err := json.Marshal(rec)
		if err != nil {
			continue
		}

		devsKey := devIndexKey(hash, op)
		pipe.Set(ctx, metaKey(hash), metaBytes, a.ttl)
		pipe.Set(ctx, devPingKey(hash, op, dev), recBytes, a.ttl)
		pipe.SAdd(ctx, devsKey, dev)
		pipe.Expire(ctx, devsKey, a.ttl)
		pipe.SAdd(ctx, indexSetKey, hash)
		pipe.SAdd(ctx, activeOpsKey, string(op))
		pipe.Expire(ctx, indexSetKey, a.ttl*2)
		pipe.Expire(ctx, activeOpsKey, a.ttl*2)

		if host != "" && s.Port >= 1 && s.Port <= 65535 {
			fp := fingerprint(host, s.Port, match)
			pipe.SAdd(ctx, fpIndexKey, fp)
			pipe.Expire(ctx, fpIndexKey, a.ttl*2)
			pipe.SAdd(ctx, fpHashesKey(fp), hash)
			pipe.Expire(ctx, fpHashesKey(fp), a.ttl*2)
		}
		accepted++
	}

	if accepted == 0 {
		return 0, fmt.Errorf("no valid samples")
	}
	if _, err := pipe.Exec(ctx); err != nil {
		return 0, fmt.Errorf("redis pipeline: %w", err)
	}
	return accepted, nil
}

// Ranked builds the app leaderboard (per config_hash), averaging devices.
func (a *Aggregator) Ranked(ctx context.Context) (models.RankedListResponse, error) {
	out := models.RankedListResponse{
		GeneratedAt: time.Now().UTC(),
		Configs:     []models.RankedConfig{},
	}

	hashes, err := a.rdb.SMembers(ctx, indexSetKey).Result()
	if err != nil && err != redis.Nil {
		return out, err
	}
	if len(hashes) == 0 {
		return out, nil
	}

	activeOps, err := a.activeOperators(ctx)
	if err != nil {
		return out, err
	}
	if len(activeOps) == 0 {
		activeOps = append([]models.Operator(nil), models.AllOperators...)
	}

	ranked := make([]models.RankedConfig, 0, len(hashes))
	stale := make([]string, 0)
	cutoff := time.Now().Add(-a.ttl).UnixMilli()

	for _, hash := range hashes {
		metaRaw, err := a.rdb.Get(ctx, metaKey(hash)).Bytes()
		if err == redis.Nil {
			stale = append(stale, hash)
			continue
		}
		if err != nil {
			return out, err
		}
		var meta configMeta
		if err := json.Unmarshal(metaRaw, &meta); err != nil {
			stale = append(stale, hash)
			continue
		}

		opPings := make([]models.OperatorPing, 0, len(models.AllOperators))
		success := 0
		sum := 0.0
		latest := int64(0)

		for _, op := range models.AllOperators {
			avg, updated, has := a.avgDevicePings(ctx, hash, op, cutoff)
			opPing := models.OperatorPing{
				Operator:  op,
				LabelFA:   op.LabelFA(),
				PingMs:    -1,
				UpdatedAt: updated,
				HasSample: has,
			}
			if has && avg > 0 {
				opPing.PingMs = int64(math.Round(avg))
				success++
				sum += avg
			} else if has {
				opPing.PingMs = -1
			}
			if updated > latest {
				latest = updated
			}
			opPings = append(opPings, opPing)
		}

		activeSuccess := 0
		activeSum := 0.0
		activeSet := map[models.Operator]struct{}{}
		for _, op := range activeOps {
			activeSet[op] = struct{}{}
		}
		for _, op := range opPings {
			if _, ok := activeSet[op.Operator]; !ok {
				continue
			}
			if op.HasSample && op.PingMs > 0 {
				activeSuccess++
				activeSum += float64(op.PingMs)
			}
		}

		rate := float64(activeSuccess) / float64(len(activeOps))
		if rate > 1 {
			rate = 1
		}
		avg := math.Inf(1)
		if activeSuccess > 0 {
			avg = activeSum / float64(activeSuccess)
		}

		updated := time.UnixMilli(latest).UTC()
		if latest == 0 {
			updated = out.GeneratedAt
		}

		ranked = append(ranked, models.RankedConfig{
			ConfigHash:    meta.ConfigHash,
			Remark:        meta.Remark,
			Protocol:      meta.Protocol,
			Transport:     meta.Transport,
			Security:      meta.Security,
			Host:          meta.Host,
			Port:          meta.Port,
			MatchHost:     meta.MatchHost,
			SuccessRate:   rate,
			AvgPing:       avg,
			OperatorPings: opPings,
			UpdatedAt:     updated,
			DeviceCount:   a.countDevices(ctx, hash, cutoff),
		})
	}

	if len(stale) > 0 {
		_ = a.rdb.SRem(ctx, indexSetKey, stringSliceToInterface(stale)...)
	}

	sort.SliceStable(ranked, func(i, j int) bool {
		if ranked[i].SuccessRate != ranked[j].SuccessRate {
			return ranked[i].SuccessRate > ranked[j].SuccessRate
		}
		ai, aj := ranked[i].AvgPing, ranked[j].AvgPing
		if math.IsInf(ai, 1) && math.IsInf(aj, 1) {
			return ranked[i].Remark < ranked[j].Remark
		}
		if math.IsInf(ai, 1) {
			return false
		}
		if math.IsInf(aj, 1) {
			return true
		}
		if ai != aj {
			return ai < aj
		}
		return ranked[i].Remark < ranked[j].Remark
	})

	for i := range ranked {
		ranked[i].Rank = i + 1
		if math.IsInf(ranked[i].AvgPing, 1) {
			ranked[i].AvgPing = -1
		} else {
			ranked[i].AvgPing = math.Round(ranked[i].AvgPing*10) / 10
		}
		ranked[i].SuccessRate = math.Round(ranked[i].SuccessRate*1000) / 1000
	}

	out.Configs = ranked
	out.Count = len(ranked)
	return out, nil
}

// RankedFingerprints averages all devices across hashes that share host:port:match_host.
// Used to reorder Astra config-order page.
func (a *Aggregator) RankedFingerprints(ctx context.Context) ([]models.FingerprintRank, error) {
	fps, err := a.rdb.SMembers(ctx, fpIndexKey).Result()
	if err != nil && err != redis.Nil {
		return nil, err
	}
	cutoff := time.Now().Add(-a.ttl).UnixMilli()
	out := make([]models.FingerprintRank, 0, len(fps))

	for _, fp := range fps {
		parts := strings.SplitN(fp, "|", 3)
		if len(parts) != 3 {
			continue
		}
		host := parts[0]
		var port int
		fmt.Sscanf(parts[1], "%d", &port)
		match := parts[2]
		if host == "" || port < 1 {
			continue
		}

		hashes, _ := a.rdb.SMembers(ctx, fpHashesKey(fp)).Result()
		if len(hashes) == 0 {
			continue
		}

		// deviceID → best positive ping in the window (across hashes/operators)
		bestByDevice := map[string]float64{}
		var label string
		var sampleN int

		for _, hash := range hashes {
			metaRaw, err := a.rdb.Get(ctx, metaKey(hash)).Bytes()
			if err == nil {
				var meta configMeta
				if json.Unmarshal(metaRaw, &meta) == nil && label == "" && meta.Remark != "" {
					label = meta.Remark
				}
			}
			for _, op := range models.AllOperators {
				devs, err := a.rdb.SMembers(ctx, devIndexKey(hash, op)).Result()
				if err != nil || len(devs) == 0 {
					continue
				}
				for _, dev := range devs {
					raw, err := a.rdb.Get(ctx, devPingKey(hash, op, dev)).Bytes()
					if err != nil {
						continue
					}
					var rec pingRecord
					if json.Unmarshal(raw, &rec) != nil {
						continue
					}
					if rec.UpdatedAt > 0 && rec.UpdatedAt < cutoff {
						continue
					}
					sampleN++
					if rec.PingMs <= 0 {
						continue
					}
					id := rec.DeviceID
					if id == "" {
						id = dev
					}
					ms := float64(rec.PingMs)
					if prev, ok := bestByDevice[id]; !ok || ms < prev {
						bestByDevice[id] = ms
					}
				}
			}
		}

		avg := -1.0
		if len(bestByDevice) > 0 {
			var sum float64
			for _, ms := range bestByDevice {
				sum += ms
			}
			avg = math.Round((sum/float64(len(bestByDevice)))*10) / 10
		}
		out = append(out, models.FingerprintRank{
			Host:        host,
			Port:        port,
			MatchHost:   match,
			Label:       label,
			AvgPing:     avg,
			SampleCount: sampleN,
			DeviceCount: len(bestByDevice),
		})
	}

	sort.SliceStable(out, func(i, j int) bool {
		ai, aj := out[i].AvgPing, out[j].AvgPing
		// positive first (lower better), then failures
		if ai > 0 && aj > 0 {
			if ai != aj {
				return ai < aj
			}
			return out[i].Host < out[j].Host
		}
		if ai > 0 {
			return true
		}
		if aj > 0 {
			return false
		}
		return out[i].Host < out[j].Host
	})
	return out, nil
}

func (a *Aggregator) avgDevicePings(
	ctx context.Context,
	hash string,
	op models.Operator,
	cutoff int64,
) (avg float64, latest int64, has bool) {
	devs, err := a.rdb.SMembers(ctx, devIndexKey(hash, op)).Result()
	if err != nil || len(devs) == 0 {
		return 0, 0, false
	}
	var sum float64
	var n int
	for _, dev := range devs {
		raw, err := a.rdb.Get(ctx, devPingKey(hash, op, dev)).Bytes()
		if err != nil {
			continue
		}
		var rec pingRecord
		if json.Unmarshal(raw, &rec) != nil {
			continue
		}
		if rec.UpdatedAt > 0 && rec.UpdatedAt < cutoff {
			continue
		}
		has = true
		if rec.UpdatedAt > latest {
			latest = rec.UpdatedAt
		}
		if rec.PingMs > 0 {
			sum += float64(rec.PingMs)
			n++
		}
	}
	if n == 0 {
		return 0, latest, has
	}
	return sum / float64(n), latest, true
}

func (a *Aggregator) countDevices(ctx context.Context, hash string, cutoff int64) int {
	seen := map[string]struct{}{}
	for _, op := range models.AllOperators {
		devs, err := a.rdb.SMembers(ctx, devIndexKey(hash, op)).Result()
		if err != nil {
			continue
		}
		for _, dev := range devs {
			raw, err := a.rdb.Get(ctx, devPingKey(hash, op, dev)).Bytes()
			if err != nil {
				continue
			}
			var rec pingRecord
			if json.Unmarshal(raw, &rec) != nil {
				continue
			}
			if rec.UpdatedAt > 0 && rec.UpdatedAt < cutoff {
				continue
			}
			id := rec.DeviceID
			if id == "" {
				id = dev
			}
			seen[id] = struct{}{}
		}
	}
	return len(seen)
}

func (a *Aggregator) activeOperators(ctx context.Context) ([]models.Operator, error) {
	vals, err := a.rdb.SMembers(ctx, activeOpsKey).Result()
	if err != nil && err != redis.Nil {
		return nil, err
	}
	ops := make([]models.Operator, 0, len(vals))
	seen := map[models.Operator]struct{}{}
	for _, v := range vals {
		op := models.Operator(strings.ToLower(strings.TrimSpace(v)))
		if !op.Valid() {
			continue
		}
		if _, ok := seen[op]; ok {
			continue
		}
		seen[op] = struct{}{}
		ops = append(ops, op)
	}
	return ops, nil
}

func metaKey(hash string) string {
	return metaKeyPrefix + hash
}

func devPingKey(hash string, op models.Operator, device string) string {
	return fmt.Sprintf("%s%s:%s:%s", devPingKeyPrefix, hash, string(op), device)
}

func devIndexKey(hash string, op models.Operator) string {
	return fmt.Sprintf("cfg:devs:%s:%s", hash, string(op))
}

func fpHashesKey(fp string) string {
	return "cfg:fphashes:" + fp
}

func stringSliceToInterface(in []string) []interface{} {
	out := make([]interface{}, len(in))
	for i, v := range in {
		out[i] = v
	}
	return out
}
