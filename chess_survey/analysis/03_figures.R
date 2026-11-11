library(tidyverse)
library(patchwork)

out_dir <- "tables_and_charts"
paper_dir <- "../../paper/Figures"
dir.create(out_dir, showWarnings = FALSE)

d <- read_csv("data/built/survey_sample.csv", show_col_types = FALSE)
n <- nrow(d)

theme_paper <- theme_classic(base_size = 10) +
  theme(panel.grid.major.y = element_line(colour = "grey88", linewidth = 0.3), axis.text = element_text(colour = "black"),
        legend.title = element_blank(), legend.key.size = unit(0.4, "cm"), plot.title = element_text(size = 10, hjust = 0.5))
pct_axis <- scale_y_continuous(limits = c(0, 100), breaks = seq(0, 100, 20), expand = expansion(mult = c(0, 0.02)))
share <- \(x) mutate(x, p = n / !!n, lo = pmax(0, 100 * (p - 1.96 * sqrt(p * (1 - p) / !!n))), hi = 100 * (p + 1.96 * sqrt(p * (1 - p) / !!n)))
binom_p <- \(k, m) binom.test(k, m)$p.value
p_label <- \(p) if (p < .001) "italic(p) < 0.001" else sprintf("italic(p) == '%.3f'", p)
cols <- c(win = "#3CB371", loss = "#C0392B", other = "#A0A0A0")

panel <- function(answers, levels, labels, fill, title, p) {
  tibble(a = factor(answers, levels)) %>%
    count(a, .drop = FALSE) %>%
    share() %>%
    ggplot(aes(a, 100 * p)) +
    geom_col(fill = fill, colour = "grey30", linewidth = 0.2, width = 0.6) +
    geom_errorbar(aes(ymin = lo, ymax = hi), width = 0.12, linewidth = 0.5) +
    annotate("text", x = mean(seq_along(levels)), y = 93, label = p_label(p), parse = TRUE, size = 3.4) +
    scale_x_discrete(labels = labels) + pct_axis + labs(x = NULL, y = NULL, title = title) + theme_paper
}

helpful <- panel(d$helps_more, c("lost", "won", "both", "neither"), c("Loss", "Win", "Both\nequally", "Neither"),
                 cols[c("loss", "win", "other", "other")], "(a) More helpful",
                 binom_p(sum(d$helps_more == "lost"), sum(d$helps_more %in% c("lost", "won")))) +
  labs(y = "Share of respondents (%)")
pleasant <- panel(d$feels_better, c("lost", "won", "nodiff"), c("Loss", "Win", "No\ndifference"),
                  cols[c("loss", "win", "other")], "(b) More pleasant",
                  binom_p(sum(d$feels_better == "won"), sum(d$feels_better %in% c("won", "lost"))))
time <- d %>%
  pivot_longer(c(time_wins, time_losses), names_to = "games", values_to = "v") %>%
  mutate(games = factor(games, c("time_losses", "time_wins"), c("Losses", "Wins")),
         v = factor(v, c(-1, 0, 1), c("Less", "Same", "More"))) %>%
  count(games, v, .drop = FALSE) %>%
  share() %>%
  ggplot(aes(v, 100 * p, colour = games)) +
  geom_errorbar(aes(ymin = lo, ymax = hi), position = position_dodge(0.3), width = 0.12, linewidth = 0.5) +
  geom_point(position = position_dodge(0.3), size = 3.2) +
  annotate("text", x = 2, y = 93, label = p_label(binom_p(sum(d$shift > 0), sum(d$shift != 0))), parse = TRUE, size = 3.4) +
  scale_colour_manual(values = unname(cols[c("loss", "win")])) +
  pct_axis + labs(x = NULL, y = NULL, title = "(c) Wish to spend ... time") + theme_paper +
  coord_cartesian(clip = "off") +
  theme(legend.position = "inside", legend.position.inside = c(0.2, 0.65))

file <- file.path(out_dir, "chess_survey_all_answers.pdf")
ggsave(file, helpful + pleasant + time, width = 7.4, height = 2.9, device = cairo_pdf)
if (dir.exists(paper_dir)) invisible(file.copy(file, paper_dir, overwrite = TRUE))
