create table public.lens_openai_fvg (
 candle_time timestamptz primary key, gap_time timestamptz not null check (gap_time < candle_time),
 side text not null check (side in ('CALL','PUT')),
 range_low numeric not null, range_high numeric not null check (range_high > range_low),
 entry numeric not null, stop numeric not null, target numeric not null,
 review_status text not null default 'pending' check (review_status in ('pending','watch','wait','expired','review_failed')),
 check ((side='CALL' and stop < entry and target=entry+10) or
        (side='PUT' and stop > entry and target=entry-10))
);
alter table public.lens_openai_fvg enable row level security;
revoke all on public.lens_openai_fvg from public, anon, authenticated;
grant select,insert,update on public.lens_openai_fvg to service_role;

create function public.lens_fvg_structure(as_of timestamptz)
returns table(bucket timestamptz, high numeric, low numeric)
language sql stable set search_path='' as $$
 select date_bin(interval '4 hours', c.closed_at - interval '1 second', timestamptz '2000-01-01 00:00+00') as bucket,
        max(c.high), min(c.low)
 from public.lens_candles_5m c
 where c.symbol='CAPITALCOM:SPX500' and c.closed_at <= as_of and c.closed_at > as_of - interval '30 days'
 and mod(extract(epoch from c.closed_at),300)=0
 group by 1 having count(*)=48 order by 1
$$;
revoke all on function public.lens_fvg_structure(timestamptz) from public, anon, authenticated;
grant execute on function public.lens_fvg_structure(timestamptz) to service_role;

create view public.lens_openai_fvg_stats with (security_invoker=true) as
with outcomes as (
 select f.*, coalesce(m.hit, case when m.n=60 then 'timeout'
     when now() >= f.candle_time+interval '70 minutes' then 'incomplete' else 'open' end) as outcome
 from public.lens_openai_fvg f
 cross join lateral (
   select count(*) as n, (array_agg(hit order by closed_at) filter(where hit is not null))[1] as hit
   from (
     select c.closed_at, row_number() over(order by c.closed_at) as seq,
         case when (f.side='CALL' and c.low<=f.stop) or (f.side='PUT' and c.high>=f.stop) then 'loss'
              when (f.side='CALL' and c.high>=f.target) or (f.side='PUT' and c.low<=f.target) then 'win' end as hit
     from public.lens_gemini_candles_1m c where c.closed_at > f.candle_time
         and c.closed_at <= f.candle_time+interval '60 minutes'
   ) b where b.closed_at=f.candle_time+b.seq*interval '1 minute'
 ) m
)
select p.period,date_trunc(p.unit,candle_time at time zone 'America/New_York')::date as period_start,
 side,review_status,count(*) as candidates,count(*) filter(where outcome='win') as wins,
 count(*) filter(where outcome='loss') as losses,count(*) filter(where outcome='timeout') as timeouts,
 count(*) filter(where outcome='incomplete') as incomplete,count(*) filter(where outcome='open') as open,
 round(100.0*count(*) filter(where outcome='win')/nullif(count(*) filter(where outcome in ('win','loss')),0),2) as win_rate_pct
 from outcomes cross join (values ('daily','day'),('weekly','week')) p(period,unit)
 group by 1,2,3,4;
revoke all on public.lens_openai_fvg_stats from public, anon, authenticated;
grant select on public.lens_openai_fvg_stats to service_role;
