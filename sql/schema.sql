-- public.exchange_rates definition

-- Drop table

-- DROP TABLE public.exchange_rates;

CREATE TABLE public.exchange_rates (
	currency_code varchar(10) NOT NULL,
	"date" date NOT NULL,
	exchange_rate numeric(8, 2) NOT NULL,
	CONSTRAINT exchange_id PRIMARY KEY (currency_code, date)
);


-- public.sellers definition

-- Drop table

-- DROP TABLE public.sellers;

CREATE TABLE public.sellers (
	seller_id int8 NOT NULL,
	seller_rating numeric(10, 2) NOT NULL,
	review_count int4 NOT NULL,
	CONSTRAINT sellers_pkey PRIMARY KEY (seller_id)
);


-- public.goods definition

-- Drop table

-- DROP TABLE public.goods;

CREATE TABLE public.goods (
	game_id int8 NOT NULL,
	title varchar(150) NOT NULL,
	description text NOT NULL,
	brand varchar(150) NOT NULL,
	photo_count int4 NOT NULL,
	"condition" varchar(30) NOT NULL,
	category varchar(30) NOT NULL,
	likes_count int4 NOT NULL,
	listed_date date NOT NULL,
	price numeric(8, 2) NOT NULL,
	price_with_protection numeric(8, 2) NOT NULL,
	shipping_cost numeric(8, 2) NOT NULL,
	currency_code varchar(10) NOT NULL,
	status varchar(10) DEFAULT 'active'::character varying NULL,
	sold_date date NULL,
	seller_id int8 NOT NULL,
	CONSTRAINT goods_pkey PRIMARY KEY (game_id),
	CONSTRAINT goods_seller_id_fkey FOREIGN KEY (seller_id) REFERENCES public.sellers(seller_id)
);


-- public.item_metrics_history definition

-- Drop table

-- DROP TABLE public.item_metrics_history;

CREATE TABLE public.item_metrics_history (
	game_id int8 NOT NULL,
	"date" date NOT NULL,
	price numeric(8, 2) NOT NULL,
	price_with_protection numeric(8, 2) NOT NULL,
	shipping_cost numeric(8, 2) NOT NULL,
	likes_count int4 NOT NULL,
	CONSTRAINT item_metrics_history_id PRIMARY KEY (game_id, date),
	CONSTRAINT item_metrics_history_game_id_fkey FOREIGN KEY (game_id) REFERENCES public.goods(game_id)
);


-- public.rating_changes definition

-- Drop table

-- DROP TABLE public.rating_changes;

CREATE TABLE public.rating_changes (
	seller_id int8 NOT NULL,
	"date" date NOT NULL,
	seller_rating numeric(10, 2) NOT NULL,
	review_count int4 NOT NULL,
	CONSTRAINT rating_change_id PRIMARY KEY (seller_id, date),
	CONSTRAINT rating_changes_seller_id_fkey FOREIGN KEY (seller_id) REFERENCES public.sellers(seller_id)
);