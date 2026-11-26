begin;

create table public.lens_openai_orb (
    session_date date not null,
    side text not null check (side in ('CALL', 'PUT')),
    candle_time timestamptz not null,
    range_high numeric not null,
    range_low numeric not null,
    entry numeric not null,
    stop numeric not null,
    target numeric not null,
    review_status text not null default 'pending' check (review_status in (
        'pending', 'sent', 'wait', 'expired', 'review_failed', 'delivery_unconfirmed'
    )),
    outcome text check (outcome in ('win', 'loss', 'timeout', 'incomplete')),
    returned_within_five boolean,
    primary key (session_date, side),
    check (range_high > range_low and (
        (side = 'CALL' and stop < entry and entry < target)
        or (side = 'PUT' and target < entry and entry < stop)
    ))
);

alter table public.lens_openai_orb enable row level security;
revoke all on public.lens_openai_orb from public, anon, authenticated;
grant select, insert, update on public.lens_openai_orb to service_role;

create function public.reconcile_lens_openai_orb()
returns void
language sql
set search_path = ''
as $$
    with evaluated as (
        select o.session_date, o.side, o.candle_time,
               (array_agg(b.hit order by b.closed_at) filter (where b.hit is not null))[1] as hit,
               count(b.closed_at) as complete_count,
               bool_or(b.inside) filter (
                   where b.closed_at <= o.candle_time + interval '5 minutes') as early_return
          from public.lens_openai_orb o
          left join lateral (
              select n.closed_at,
                     n.close between o.range_low and o.range_high as inside,
                     case
                         when (o.side = 'CALL' and n.low <= o.stop)
                           or (o.side = 'PUT' and n.high >= o.stop) then 'loss'
                         when (o.side = 'CALL' and n.high >= o.target)
                           or (o.side = 'PUT' and n.low <= o.target) then 'win'
                     end as hit
                from (
                    select c.*, row_number() over (order by c.closed_at) as position
                      from public.lens_gemini_candles_1m c
                     where c.closed_at > o.candle_time
                       and c.closed_at <= o.candle_time + interval '60 minutes'
                ) n
               where n.closed_at = o.candle_time + n.position * interval '1 minute'
          ) b on true
         where o.outcome is null or (o.returned_within_five is null
               and now() < o.candle_time + interval '70 minutes')
         group by o.session_date, o.side, o.candle_time
    )
    update public.lens_openai_orb o
       set outcome = coalesce(o.outcome, e.hit, case
               when e.complete_count = 60 then 'timeout'
               when now() >= e.candle_time + interval '70 minutes' then 'incomplete'
           end),
           returned_within_five = coalesce(o.returned_within_five, case
               when e.early_return then true
               when e.complete_count >= 5 then false
           end)
      from evaluated e
     where (o.session_date, o.side) = (e.session_date, e.side)
       and (e.hit is not null or e.early_return or e.complete_count >= 5
            or now() >= e.candle_time + interval '70 minutes');
$$;

revoke all on function public.reconcile_lens_openai_orb() from public, anon, authenticated;
grant execute on function public.reconcile_lens_openai_orb() to service_role;

create view public.lens_openai_orb_stats with (security_invoker = true) as
select p.period, date_trunc(p.unit, o.session_date::timestamp)::date as period_start,
       o.side, o.review_status, count(*) as candidates,
       count(*) filter (where outcome = 'win') as wins,
       count(*) filter (where outcome = 'loss') as losses,
       count(*) filter (where outcome = 'timeout') as timeouts,
       count(*) filter (where outcome = 'incomplete') as incomplete,
       count(*) filter (where outcome is null) as open,
       count(*) filter (where returned_within_five) as early_returns,
       count(returned_within_five) as early_return_observed,
       round(100.0 * count(*) filter (where outcome = 'win')
             / nullif(count(*) filter (where outcome in ('win', 'loss')), 0), 2) as win_rate_pct
  from public.lens_openai_orb o
 cross join (values ('daily', 'day'), ('weekly', 'week')) as p(period, unit)
 group by 1, 2, 3, 4;

revoke all on public.lens_openai_orb_stats from public, anon, authenticated;
grant select on public.lens_openai_orb_stats to service_role;

commit;
