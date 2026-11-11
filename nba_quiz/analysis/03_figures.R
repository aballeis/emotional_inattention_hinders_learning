library(tidyverse)
library(patchwork)
library(sandwich)

out_dir <- "tables_and_charts"
paper_dir <- "../../paper/Figures/nba"
dir.create(out_dir, showWarnings = FALSE)

save_fig <- function(p, name, w, h) {
  file <- file.path(out_dir, paste0(name, ".pdf"))
  ggsave(file, p, width = w, height = h, device = cairo_pdf)
  if (dir.exists(paper_dir)) invisible(file.copy(file, paper_dir, overwrite = TRUE))
}

cols <- c(loss = "#C0392B", win = "#3CB371", indifferent = "#A0A0A0")
pref_labels <- c(loss = "Prefer to review the loss", win = "Prefer to review the win", indifferent = "No preference")
theme_paper <- theme_classic(base_size = 10) +
  theme(panel.grid.major.y = element_line(colour = "grey88", linewidth = 0.3),
        axis.text = element_text(colour = "black"), legend.position = "bottom", legend.title = element_blank(),
        legend.key.size = unit(0.4, "cm"), plot.title = element_text(size = 10, hjust = 0.5))
pct_axis <- scale_y_continuous(limits = c(0, 100), breaks = seq(0, 100, 20), expand = expansion(mult = c(0, 0.02)))
bars <- \(...) geom_col(..., colour = "grey30", linewidth = 0.2)
ci <- \(...) geom_errorbar(aes(ymin = pmax(0, 100 * (p - 1.96 * se)), ymax = 100 * (p + 1.96 * se)), ..., width = 0.15, linewidth = 0.5)
share <- \(x) mutate(x, p = n / sum(n), se = sqrt(p * (1 - p) / sum(n)))

d <- read_csv("data/built/participant_level_data.csv", show_col_types = FALSE) %>%
  filter(in_sample) %>%
  mutate(pref = factor(review_pref, names(cols)), pref_win_c = pref_win - mean(pref_win), pref_loss_c = pref_loss - mean(pref_loss))

# ---- Review preferences ----

overall <- d %>% count(pref) %>% share()
beliefs <- c(loss = "Loss helps more", win = "Win helps more", "no difference" = "No difference")
belief_n <- count(d, helpful)
belief_labels <- sprintf("%s\n(n = %d)", beliefs, belief_n$n[match(names(beliefs), belief_n$helpful)])
by_belief <- d %>%
  mutate(belief = factor(helpful, names(beliefs), belief_labels)) %>%
  count(belief, pref) %>%
  group_by(belief) %>%
  share() %>%
  ungroup()

p_all <- ggplot(overall, aes(pref, 100 * p, fill = pref)) +
  bars(width = 0.8) + ci() +
  scale_x_discrete(labels = NULL) +
  scale_fill_manual(values = cols, labels = pref_labels, drop = FALSE) +
  pct_axis + labs(x = NULL, y = "Share of participants (%)", title = sprintf("All participants (N = %d)", nrow(d))) + theme_paper +
  theme(axis.ticks.x = element_blank())
p_belief <- ggplot(by_belief, aes(belief, 100 * p, fill = pref)) +
  bars(position = position_dodge(0.85), width = 0.8) + ci(position = position_dodge(0.85)) +
  scale_fill_manual(values = cols, labels = pref_labels) +
  pct_axis + labs(x = NULL, y = NULL, title = "By belief") + theme_paper
save_fig((p_all + p_belief) + plot_layout(widths = c(1, 1.6), guides = "collect") & theme(legend.position = "bottom"),
         "nba_review_preferences", 6.5, 3.2)

# ---- Stated reasons ----

reasons <- c(hedonic = "Hedonic", instrumental = "Instrumental", curiosity = "Curiosity",
             team = "Team", indifference = "Indifference", other = "Other")
labels <- read_csv("data/llm_labels_fable-5-1.csv", show_col_types = FALSE) %>%
  semi_join(d, by = "pid") %>%
  mutate(pref = factor(pref, names(cols)))
n_pref <- count(labels, pref)
reason_shares <- labels %>%
  pivot_longer(names(reasons), names_to = "reason", values_to = "y") %>%
  group_by(pref, reason) %>%
  summarise(p = mean(y), se = sqrt(p * (1 - p) / n()), .groups = "drop") %>%
  mutate(reason = factor(reason, names(reasons), reasons))
p_reasons <- ggplot(reason_shares, aes(reason, 100 * p, fill = pref)) +
  bars(position = position_dodge(0.85), width = 0.8) + ci(position = position_dodge(0.85)) +
  scale_fill_manual(values = cols, labels = sprintf("%s\n(n = %d)", pref_labels[as.character(n_pref$pref)], n_pref$n)) +
  pct_axis + labs(x = NULL, y = "Share of responses (%)") + theme_paper
save_fig(p_reasons, "nba_review_reasons", 6.5, 3.2)

# ---- Confidence ----

conf <- d %>%
  mutate(stated = post_r2_confidence_percent,
         actual = 100 * as.numeric(post_r2_confidence_performance_success),
         over = stated - actual,
         assign = factor(review_assignment, c("loss", "win"), c("Reviewed the loss", "Reviewed the win")))
outcomes <- c(stated = "Stated probability of\nwinning a majority", actual = "Share actually\nwinning a majority",
              over = "Overconfidence\n(stated minus actual)")
p_value <- function(v) {
  m <- lm(reformulate(c("assigned_loss", "pref_win_c", "pref_loss_c", "assigned_loss:pref_win_c", "assigned_loss:pref_loss_c"), v), conf)
  b <- coef(m)[2] / sqrt(vcovHC(m, type = "HC1")[2, 2])
  2 * pt(-abs(b), df.residual(m))
}
conf_means <- conf %>%
  pivot_longer(names(outcomes), names_to = "outcome", values_to = "v") %>%
  group_by(outcome, assign) %>%
  summarise(mean = mean(v), se = sd(v) / sqrt(n()), .groups = "drop") %>%
  mutate(outcome = factor(outcome, names(outcomes), outcomes))
conf_p <- conf_means %>%
  group_by(outcome) %>%
  summarise(top = max(mean + 1.96 * se) + 3, .groups = "drop") %>%
  mutate(label = sprintf("italic(p) == '%.2f'", map_dbl(names(outcomes), p_value)))
p_conf <- ggplot(conf_means, aes(outcome, mean)) +
  geom_col(aes(fill = assign), position = position_dodge(0.75), width = 0.7, colour = "grey30", linewidth = 0.2) +
  geom_errorbar(aes(group = assign, ymin = mean - 1.96 * se, ymax = mean + 1.96 * se), position = position_dodge(0.75), width = 0.12, linewidth = 0.5) +
  geom_text(data = conf_p, aes(y = top, label = label), parse = TRUE, vjust = 0, size = 3.4) +
  scale_fill_manual(values = unname(cols[c("loss", "win")])) +
  pct_axis + labs(x = NULL, y = "Percent") + theme_paper
save_fig(p_conf, "nba_confidence", 5.9, 3.0)
