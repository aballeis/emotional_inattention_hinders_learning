library(tidyverse)
library(sandwich)

paper_dir <- "../../paper"

participants <- read_csv("data/built/participant_level_data.csv", show_col_types = FALSE)
responses <- read_csv("data/built/response_level_data.csv", show_col_types = FALSE)
labels <- read_csv("data/llm_labels_fable-5-1.csv", show_col_types = FALSE)
labels_robust <- read_csv("data/llm_labels_haiku-4-5.csv", show_col_types = FALSE)

d <- participants %>%
  filter(in_sample) %>%
  mutate(pref_win_c = pref_win - mean(pref_win), pref_loss_c = pref_loss - mean(pref_loss))

pc <- \(x, k = 0) formatC(100 * x, format = "f", digits = k)
f2 <- \(x) sprintf("%.2f", x)
f_p <- \(p, k = 2) if (p < .001) "$p<0.001$" else paste0("$p=", formatC(p, format = "f", digits = k), "$")
f_int <- \(x) format(x, big.mark = ",", trim = TRUE)

hc1 <- function(f, x = d) {
  m <- lm(f, x)
  V <- vcovHC(m, type = "HC1")
  list(m = m, b = coef(m), se = sqrt(diag(V)), p = 2 * pt(-abs(coef(m) / sqrt(diag(V))), df.residual(m)), V = V)
}
prereg <- \(y) reformulate(c("assigned_loss", "pref_win_c", "pref_loss_c", "assigned_loss:pref_win_c", "assigned_loss:pref_loss_c"), y)

tex <- if (dir.exists(paper_dir)) {
  list.files(paper_dir, "\\.tex$", full.names = TRUE) %>%
    set_names(basename) %>%
    map(\(f) str_squish(paste(readLines(f, warn = FALSE), collapse = " ")))
}
rows <- list()
add <- function(section, file, statistic, value, raw = "", snippet = value) {
  found <- if (is.null(tex)) "paper not found" else if (str_detect(tex[[file]], fixed(snippet))) "yes" else paste0("NO: expected `", snippet, "`")
  rows[[length(rows) + 1]] <<- tibble(section, location = str_remove(file, "\\.tex$"), statistic, value, raw, found)
}
intro <- "Sec 1 Intro.tex"
nba <- "Sec 4 NBA.tex"
llm <- "Sec A3 Classifier Prompt.tex"

# ---- Sample ----

flow <- c("passed_attention", "attempted_comprehension", "passed_comprehension", "completed_r1", "win_and_loss_r1",
          "completed_r2", "closing_questions", "no_lookup")
remaining <- accumulate(flow, \(s, v) s & participants[[v]], .init = rep(TRUE, nrow(participants))) %>% map_int(sum) %>% set_names(c("started", flow))
dates <- as.Date(d$start_date)
add("Sample", intro, "Analysis sample", sprintf("$N=%d$", nrow(d)))
add("Sample", nba, "Recruitment dates", sprintf("from August %s to %s, 2026", format(min(dates), "%d"), format(max(dates), "%d")))
bonus <- responses %>% group_by(pid) %>% summarise(wins = sum(outcome == "won")) %>%
  right_join(select(d, pid, post_r2_confidence_bonus_amount), by = "pid") %>%
  mutate(bonus = 0.10 * replace_na(wins, 0) + replace_na(post_r2_confidence_bonus_amount, 0))
add("Sample", nba, "Average bonus", sprintf("the average bonus was \\$%.2f", mean(bonus$bonus)), sprintf("%.4f", mean(bonus$bonus)))
dur <- median(as.numeric(d$duration_in_seconds)) / 60
add("Sample", nba, "Median duration (minutes)", sprintf("the median duration %.0f minutes", dur), sprintf("%.2f", dur))
att <- participants %>% filter(passed_attention, !is.na(comprehension_attempts), comprehension_attempts >= 1)
done <- !is.na(att$comprehension_completed_utc)
add("Sample", nba, "Comprehension check (footnote)",
    sprintf("Of the %s participants who made at least one attempt, %s percent passed on their first try and %s percent passed within three tries; %s percent failed all three attempts and exited the study, and the remaining %s percent quit",
            f_int(nrow(att)), pc(mean(done & att$comprehension_attempts == 1)), pc(mean(done)),
            pc(mean(!done & att$comprehension_attempts >= 3)), pc(mean(!done & att$comprehension_attempts < 3))))
add("Sample", nba, "Win and loss in Round 1, among those completing Round 1",
    sprintf("which was %s percent of those who completed Round 1", pc(remaining[["win_and_loss_r1"]] / remaining[["completed_r1"]])),
    sprintf("%d of %d", remaining[["win_and_loss_r1"]], remaining[["completed_r1"]]))
recall <- participants %>% filter(entered_app, !is.na(review_checks_completed_utc))
wrong <- filter(recall, review_margin_wrong_guesses > 0)
add("Sample", nba, "Recall check (footnote)",
    sprintf("Of the %s participants who answered the recall question, %s percent gave the correct margin on their first try; of the remaining %s, %s percent looked at the review again",
            f_int(nrow(recall)), pc(mean(recall$review_margin_wrong_guesses == 0)), f_int(nrow(wrong)), pc(mean(wrong$review_margin_used_reopen == 1))))
completed <- with(participants, passed_attention & attempted_comprehension & passed_comprehension & completed_r1 & completed_r2 & closing_questions)
add("Sample", nba, "Completed / no win and loss in Round 1 / looked up / analysis sample",
    sprintf("In total, %d participants completed the study. We exclude the %d who did not have both a won and a lost question in Round 1, and the additional %d who reported having looked up NBA games, which leaves an analysis sample of %d.",
            sum(completed), sum(completed & !participants$win_and_loss_r1), remaining[["closing_questions"]] - remaining[["no_lookup"]], remaining[["no_lookup"]]))

# ---- Causal effect of reviewing ----

r2 <- responses %>% filter(round == "r2") %>% inner_join(select(d, pid, assigned_review_game_id), by = "pid")
add("Effect of reviewing", nba, "Entered the true margin for the reviewed question in Round 2",
    sprintf("%s percent did so for the question they reviewed", pc(mean(filter(r2, game_id == assigned_review_game_id) %>% with(answer == correct)))))
offered <- d %>%
  transmute(miss_won = if_else(outcome_a == "won", participant_distance_a, participant_distance_b),
            miss_lost = if_else(outcome_a == "lost", participant_distance_a, participant_distance_b),
            beat_won = if_else(outcome_a == "won", exp_win_a, exp_win_b),
            beat_lost = if_else(outcome_a == "lost", exp_win_a, exp_win_b))
add("Effect of reviewing", nba, "Round 1 miss and chance of beating a random opponent, offered lost vs won question",
    sprintf("guesses missed by %.0f points on average on the lost question offered for review versus %.0f on the won one, and the probability of beating a random opponent was %s versus %s percent",
            mean(offered$miss_lost), mean(offered$miss_won), pc(mean(offered$beat_lost)), pc(mean(offered$beat_won))),
    sprintf("%.2f vs %.2f; %.3f vs %.3f", mean(offered$miss_lost), mean(offered$miss_won), mean(offered$beat_lost), mean(offered$beat_won)))
eff <- hc1(prereg("exp_score_dif"))
lost <- c(1, 1, 0, 0, 0, 0)
raw_eff <- sprintf("b = %.4f, SE = %.4f, p = %.2g", eff$b[2], eff$se[2], eff$p[2])
add("Effect of reviewing", intro, "Effect of reviewing the loss on expected questions won (Table col. 1)",
    sprintf("by %s more out of five", f2(eff$b[2])), raw_eff)
add("Effect of reviewing", nba, "Effect of reviewing the loss on expected questions won (Table col. 1)",
    sprintf("by %s (s.e.\\ %s, %s", f2(eff$b[2]), f2(eff$se[2]), f_p(eff$p[2])), raw_eff)
add("Effect of reviewing", nba, "Gain in expected questions won after reviewing the lost / won question",
    sprintf("expected wins rise by %s after reviewing the lost question and by %s after reviewing the won one", f2(sum(lost * eff$b)), f2(eff$b[1])))
w2 <- hc1(prereg("wins_r2"))
wm <- d %>% group_by(review_assignment) %>% summarise(m = mean(wins_r2)) %>% deframe()
add("Effect of reviewing", nba, "Round 2 questions won, assigned lost vs won question (footnote)",
    sprintf("win %s of the five questions against %s for those assigned the won question (%s)", f2(wm[["loss"]]), f2(wm[["win"]]), f_p(w2$p[2])),
    sprintf("b = %.3f, SE = %.3f", w2$b[2], w2$se[2]))

# ---- Beliefs and preferences (preregistered analyses 1-3) ----

prefs <- d %>% count(review_pref) %>% mutate(s = n / sum(n)) %>% select(-n) %>% deframe()
add("Preferences", intro, "Prefer win / loss / indifferent",
    sprintf("%s percent preferred to review the won question, %s percent the lost question, and %s percent were indifferent", pc(prefs[["win"]]), pc(prefs[["loss"]]), pc(prefs[["indifferent"]])))
add("Preferences", nba, "Prefer win / loss / indifferent",
    sprintf("%s percent prefer to review the question they won and %s percent the one they lost, with %s percent indifferent", pc(prefs[["win"]]), pc(prefs[["loss"]]), pc(prefs[["indifferent"]])))
a1 <- filter(d, review_pref %in% c("win", "loss"))
t1 <- binom.test(sum(a1$review_pref == "win"), nrow(a1))
add("Preferences", intro, "Prereg. Analysis 1: share choosing the win among those with a preference",
    sprintf("%s percent chose the won question (%s", pc(t1$estimate), f_p(t1$p.value, 3)), sprintf("%d of %d, p = %.4f", sum(a1$review_pref == "win"), nrow(a1), t1$p.value))
add("Preferences", nba, "Prereg. Analysis 1: share choosing the win among those with a preference",
    sprintf("%s percent choose the won question (%s", pc(t1$estimate), f_p(t1$p.value, 3)), sprintf("%d of %d, p = %.4f", sum(a1$review_pref == "win"), nrow(a1), t1$p.value))
beliefs <- d %>% count(helpful) %>% mutate(s = n / sum(n)) %>% select(-n) %>% deframe()
a2 <- filter(d, helpful %in% c("win", "loss"))
t2 <- binom.test(sum(a2$helpful == "loss"), nrow(a2))
add("Beliefs", intro, "Prereg. Analysis 2: believe the loss / the win helps more",
    sprintf("Almost half (%s percent) correctly believed the lost question would be more helpful and only %s percent reported that the won question would be more helpful (%s",
            pc(beliefs[["loss"]]), pc(beliefs[["win"]]), f_p(t2$p.value)), sprintf("p = %.2g", t2$p.value))
add("Beliefs", nba, "Prereg. Analysis 2: believe the loss / the win helps more",
    sprintf("%s percent say the lost question and %s percent say the won question (%s", pc(beliefs[["loss"]]), pc(beliefs[["win"]]), f_p(t2$p.value)))
wp <- d %>% filter(review_pref == "win") %>% count(helpful) %>% mutate(s = n / sum(n)) %>% select(-n) %>% deframe()
add("Beliefs", intro, "Beliefs of those who prefer to review the win: win / loss / no difference",
    sprintf("only %s percent thought it more beneficial than the lost question, while %s percent thought the lost question more beneficial and %s percent saw no difference",
            pc(wp[["win"]]), pc(wp[["loss"]]), pc(wp[["no difference"]])))
add("Preferences", nba, "Share believing the loss helps more", sprintf("even among the %s percent who believed", pc(beliefs[["loss"]])))
a3 <- filter(d, helpful == "loss", review_pref %in% c("win", "loss"))
t3 <- binom.test(sum(a3$review_pref == "win"), nrow(a3))
add("Preferences", nba, "Prereg. Analysis 3: loss-believers with a preference choosing the win",
    sprintf("(%s percent) chose to review the won question (%s against an even split", pc(t3$estimate), f_p(t3$p.value)), sprintf("%d of %d (%.1f%%), binomial p = %.2g", sum(a3$review_pref == "win"), nrow(a3), 100 * t3$estimate, t3$p.value))

# ---- Stated reasons ----

lab <- semi_join(labels, d, by = "pid")
k_hed <- sum(lab$hedonic[lab$pref == "loss"])
hedonic_loss <- \(zero) if (k_hed == 0) zero else if (k_hed == 1) "only one participant" else paste(pc(k_hed / sum(lab$pref == "loss")), "percent")
sh <- lab %>% group_by(pref) %>% summarise(across(hedonic:other, mean), n = n()) %>% split(.$pref)
add("Stated reasons", intro, "Hedonic / instrumental, prefer the win",
    sprintf("%s percent gave hedonic reasons (wanting to relive the win or avoid the loss) and %s percent instrumental reasons", pc(sh$win$hedonic), pc(sh$win$instrumental)))
add("Stated reasons", intro, "Hedonic / instrumental, prefer the loss",
    sprintf("%s gave hedonic reasons and %s percent gave instrumental ones", hedonic_loss("none"), pc(sh$loss$instrumental)),
    sprintf("hedonic: %d of %d", sum(lab$hedonic[lab$pref == "loss"]), sh$loss$n))
add("Stated reasons", nba, "Hedonic / instrumental, prefer the win",
    sprintf("hedonic motives are common (%s percent) and instrumental motives are rare (%s percent)", pc(sh$win$hedonic), pc(sh$win$instrumental)))
add("Stated reasons", nba, "Hedonic / instrumental, prefer the loss",
    sprintf("%s gives a hedonic reason and %s percent give an instrumental one", hedonic_loss("no one"), pc(sh$loss$instrumental)),
    sprintf("hedonic: %d of %d", sum(lab$hedonic[lab$pref == "loss"]), sh$loss$n))
add("Stated reasons", nba, "Curiosity, prefer the win / the loss", sprintf("(%s and %s percent)", pc(sh$win$curiosity), pc(sh$loss$curiosity)))
add("Stated reasons", nba, "Indifference, no preference", sprintf("express indifference (%s percent)", pc(sh$indifferent$indifference)))
agree <- inner_join(labels, labels_robust, by = "pid", suffix = c("", "_r"))
agree_rates <- map_dbl(c("hedonic", "instrumental", "curiosity", "team", "indifference", "other"), \(v) mean(agree[[v]] == agree[[paste0(v, "_r")]]))
add("Stated reasons", llm, "Agreement of Claude Haiku 4.5 with Claude Fable 5.1 labels, by category (min to max)",
    sprintf("%s to %s percent of the experiment answers", pc(min(agree_rates)), pc(max(agree_rates))), paste(pc(agree_rates, 1), collapse = ", "))

# ---- Confidence ----

conf <- d %>% mutate(stated = post_r2_confidence_percent, actual = 100 * as.numeric(post_r2_confidence_performance_success), over = stated - actual)
cm <- conf %>% group_by(review_assignment) %>% summarise(across(c(stated, actual, over), mean)) %>% column_to_rownames("review_assignment")
co <- hc1(prereg("over"), conf)
add("Confidence", intro, "Overconfidence, reviewed win minus reviewed loss", sprintf("by %.0f percentage points (%s)", -co$b[2], f_p(co$p[2])),
    sprintf("b = %.3f, SE = %.3f, p = %.4f", -co$b[2], co$se[2], co$p[2]))
add("Confidence", nba, "Overconfidence, reviewed win vs loss", sprintf("by %.0f percentage points (%.0f versus %.0f, %s)", -co$b[2], cm["win", "over"], cm["loss", "over"], f_p(co$p[2])),
    sprintf("%.2f vs %.2f", cm["win", "over"], cm["loss", "over"]))
add("Confidence", nba, "Stated probability, reviewed win vs loss", sprintf("(%.0f versus %.0f percent)", cm["win", "stated"], cm["loss", "stated"]),
    sprintf("%.2f vs %.2f, p = %.3f", cm["win", "stated"], cm["loss", "stated"], hc1(prereg("stated"), conf)$p[2]))
add("Confidence", nba, "Won a majority, reviewed win vs loss", sprintf("(%.0f versus %.0f percent win a majority)", cm["win", "actual"], cm["loss", "actual"]),
    sprintf("%.2f vs %.2f, p = %.3f", cm["win", "actual"], cm["loss", "actual"], hc1(prereg("actual"), conf)$p[2]))

# ---- Write ----

out <- bind_rows(rows)
md <- c("# Numbers check: NBA prediction study", "",
        "Generated by `analysis/numbers.R`. Every statistic about the NBA study quoted in the paper text, with where it appears.",
        "Regressions use HC1 standard errors; effects of the assigned review control for the centered stated preference and its",
        "interactions with the assignment (preregistered Analysis 4). \"In paper\" checks that the text in the Value column appears",
        "verbatim in that section of the paper.", "",
        sprintf("**%d of %d statistics found in the paper text.**", sum(out$found == "yes"), nrow(out)), "")
for (sec in unique(out$section)) {
  s <- filter(out, section == sec)
  md <- c(md, paste("##", sec), "", "| Location | Statistic | Value | Unrounded | In paper |", "|---|---|---|---|---|",
          sprintf("| %s | %s | %s | %s | %s |", s$location, s$statistic, s$value, s$raw, s$found), "")
}
writeLines(md, "tables_and_charts/numbers_check.md")
cat(sum(out$found == "yes"), "of", nrow(out), "found in paper\n")
